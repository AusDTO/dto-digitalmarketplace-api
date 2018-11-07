import json
import mimetypes
import os
import botocore
import rollbar
import pendulum
from pendulum.parsing.exceptions import ParserError
from flask import Response, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import DataError

from app.api import api
from app.api.csv import generate_brief_responses_csv
from app.api.business.validators import SupplierValidator
from app.api.helpers import abort, forbidden, not_found, role_required, is_current_user_in_brief
from app.api.services import (audit_service,
                              brief_overview_service,
                              brief_responses_service,
                              briefs,
                              lots_service,
                              suppliers)
from app.emails import send_brief_response_received_email, render_email_template
from dmapiclient.audit import AuditTypes
from dmutils.file import s3_download_file, s3_upload_file_from_request

from ...models import (AuditEvent, Brief, BriefResponse, Framework, Supplier,
                       ValidationError, Lot, User, db)
from ...utils import get_json_from_request


def _can_do_brief_response(brief_id):
    try:
        brief = Brief.query.get(brief_id)
    except DataError:
        brief = None

    if brief is None:
        abort("Invalid brief ID '{}'".format(brief_id))

    if brief.status != 'live':
        abort("Brief must be live")

    if brief.framework.status != 'live':
        abort("Brief framework must be live")

    if not hasattr(current_user, 'role') or current_user.role != 'supplier':
        forbidden("Only supplier role users can respond to briefs")

    try:
        supplier = Supplier.query.filter(
            Supplier.code == current_user.supplier_code
        ).first()
    except DataError:
        supplier = None

    if not supplier:
        forbidden("Invalid supplier Code '{}'".format(current_user.supplier_code))

    errors = SupplierValidator(supplier).validate_all()
    if len(errors) > 0:
        abort(errors)

    def domain(email):
        return email.split('@')[-1]

    current_user_domain = domain(current_user.email_address) \
        if domain(current_user.email_address) not in current_app.config.get('GENERIC_EMAIL_DOMAINS') \
        else None

    rfx_lot = lots_service.find(slug='rfx').one_or_none()
    rfx_lot_id = rfx_lot.id if rfx_lot else None
    if brief.data.get('sellerSelector', '') == 'someSellers' and brief.lot_id != rfx_lot_id:
        seller_domain_list = [domain(x).lower() for x in brief.data['sellerEmailList']]
        if current_user.email_address not in brief.data['sellerEmailList'] \
                and (not current_user_domain or current_user_domain.lower() not in seller_domain_list):
            forbidden("Supplier not selected for this brief")
    if brief.data.get('sellerSelector', '') == 'oneSeller' and brief.lot_id != rfx_lot_id:
        if current_user.email_address.lower() != brief.data['sellerEmail'].lower() \
                and (not current_user_domain or
                     current_user_domain.lower() != domain(brief.data['sellerEmail'].lower())):
            forbidden("Supplier not selected for this brief")
    if brief.lot_id == rfx_lot_id:
        if str(current_user.supplier_code) not in brief.data['sellers'].keys():
            forbidden("Supplier not selected for this brief")

    if (len(supplier.frameworks) == 0 or
            'digital-marketplace' != supplier.frameworks[0].framework.slug):

        abort("Supplier does not have Digital Marketplace framework")

    if len(supplier.assessed_domains) == 0:
        abort("Supplier does not have at least one assessed domain")
    else:
        training_lot = lots_service.find(slug='training').one_or_none()
        if brief.lot_id == training_lot.id:
            if 'Training, Learning and Development' not in supplier.assessed_domains:
                abort("Supplier needs to be assessed in 'Training, Learning and Development'")

    lot = lots_service.first(slug='digital-professionals')
    if brief.lot_id == lot.id:
        # Check if there are more than 3 brief response already from this supplier when professional aka specialists
        brief_response_count = brief_responses_service.find(supplier_code=supplier.code,
                                                            brief_id=brief.id,
                                                            withdrawn_at=None).count()
        if (brief_response_count > 2):  # TODO magic number
            abort("There are already 3 brief responses for supplier '{}'".format(supplier.code))
    else:
        # Check if brief response already exists from this supplier when outcome for all other types
        if brief_responses_service.find(supplier_code=supplier.code,
                                        brief_id=brief.id,
                                        withdrawn_at=None).one_or_none():
            abort("Brief response already exists for supplier '{}'".format(supplier.code))

    return supplier, brief


@api.route('/brief/rfq', methods=['POST'])
@login_required
@role_required('buyer')
def create_rfq_brief():
    if current_user.role != 'buyer':
        return forbidden('Unauthorised to create a brief')
    try:
        lot = Lot.query.filter(Lot.slug == 'rfx').first()
        framework = Framework.query.filter(Framework.slug == 'digital-marketplace').first()
        user = User.query.get(current_user.id)
        brief = Brief(
            users=[user],
            framework=framework,
            lot=lot,
            data={}
        )
        db.session.add(brief)
        db.session.commit()
    except Exception as e:
        rollbar.report_exc_info()
        return jsonify(message=e.message), 400

    return jsonify(brief.serialize(with_users=False))


@api.route('/brief/<int:brief_id>', methods=["GET"])
def get_brief(brief_id):
    brief = Brief.query.filter(
        Brief.id == brief_id
    ).first()
    if not brief:
        not_found("No brief for id '%s' found" % (brief_id))

    user_is_privileged = False
    brief_user_ids = [user.id for user in brief.users]
    if hasattr(current_user, 'role') and current_user.role == 'buyer' and current_user.id in brief_user_ids:
        user_is_privileged = True

    if brief.status == 'draft' and not user_is_privileged:
        return forbidden("Unauthorised to view brief or brief does not exist")

    brief_response_count = 0
    brief_responses = brief_responses_service.get_brief_responses(brief_id, None)
    if brief_responses:
        brief_response_count = len(brief_responses)

    invited_seller_count = 0
    if 'sellers' in brief.data and brief.data['sellers']:
        invited_seller_count = len(brief.data['sellers'])

    # is the current user an invited seller?
    is_invited_seller = False
    if ('sellers' in brief.data and
       hasattr(current_user, 'role') and
       current_user.role == 'supplier' and
       str(current_user.supplier_code) in brief.data['sellers'].keys()):
        is_invited_seller = True

    # remove private data for unprivileged users
    if not user_is_privileged:
        brief.data['sellers'] = {}
        brief.responses_zip_filesize = None

    return jsonify(brief=brief.serialize(with_users=False),
                   brief_response_count=brief_response_count,
                   invited_seller_count=invited_seller_count,
                   is_invited_seller=is_invited_seller)


@api.route('/brief/<int:brief_id>', methods=['PATCH'])
@login_required
@role_required('buyer')
def update_brief(brief_id):
    brief = briefs.get(brief_id)

    if not brief:
        not_found("Invalid brief id '{}'".format(brief_id))

    if current_user.role == 'buyer':
        brief_user_ids = [user.id for user in brief.users]
        if current_user.id not in brief_user_ids:
            return forbidden('Unauthorised to update brief')

    data = get_json_from_request()

    publish = False
    if 'publish' in data and data['publish']:
        del data['publish']
        publish = True

    if 'evaluationType' in data:
        if 'Written proposal' not in data['evaluationType']:
            data['proposalType'] = []
        if 'Response template' not in data['evaluationType']:
            data['responseTemplate'] = []

    closed_at = None
    if 'closedAt' in data and data['closedAt'] and publish:
        try:
            parsed = pendulum.parse(data['closedAt'])
            if not parsed.is_future():
                abort('The closing date must be at least 1 week into the future')
            closed_at = data['closedAt']
        except ParserError as e:
            abort('The closing date is invalid')

    if 'sellers' in data and len(data['sellers']) > 0:
        data['sellerSelector'] = 'someSellers' if len(data['sellers']) > 1 else 'oneSeller'

    if publish:
        brief.publish(closed_at=closed_at)

    brief.data = data
    db.session.add(brief)
    db.session.commit()

    return jsonify(brief.serialize(with_users=False))


@api.route('/brief/<int:brief_id>', methods=['DELETE'])
@login_required
@role_required('buyer')
def delete_brief(brief_id):
    """Delete brief (role=buyer)
    ---
    tags:
        - brief
    definitions:
        DeleteBrief:
            type: object
            properties:
                message:
                    type: string
    parameters:
      - name: brief_id
        in: path
        type: number
        required: true
    responses:
        200:
            description: Brief deleted successfully.
            schema:
                $ref: '#/definitions/DeleteBrief'
        400:
            description: Bad request. Brief status must be 'draft'.
        403:
            description: Unauthorised to delete brief.
        404:
            description: brief_id not found.
        500:
            description: Unexpected error.
    """
    brief = briefs.get(brief_id)

    if not brief:
        not_found("Invalid brief id '{}'".format(brief_id))

    if current_user.role == 'buyer':
        brief_user_ids = [user.id for user in brief.users]
        if current_user.id not in brief_user_ids:
            return forbidden('Unauthorised to delete brief')

    if brief.status != 'draft':
        abort('Cannot delete a {} brief'.format(brief.status))

    audit = AuditEvent(
        audit_type=AuditTypes.delete_brief,
        user=current_user.email_address,
        data={
            'briefId': brief_id
        },
        db_object=None
    )

    try:
        audit_service.save(audit)
        briefs.delete(brief)
    except Exception as e:
        extra_data = {'audit_type': AuditTypes.delete_brief, 'briefId': brief.id, 'exception': e.message}
        rollbar.report_exc_info(extra_data=extra_data)

    return jsonify(message='Brief {} deleted'.format(brief_id)), 200


@api.route('/brief/<int:brief_id>/overview', methods=["GET"])
@login_required
@role_required('buyer')
def get_brief_overview(brief_id):
    """Overview (role=buyer)
    ---
    tags:
        - brief
    definitions:
        BriefOverview:
            type: object
            properties:
                sections:
                    type: array
                    items:
                        $ref: '#/definitions/BriefOverviewSections'
                title:
                    type: string
        BriefOverviewSections:
            type: array
            items:
                $ref: '#/definitions/BriefOverviewSection'
        BriefOverviewSection:
            type: object
            properties:
                links:
                    type: array
                    items:
                        $ref: '#/definitions/BriefOverviewSectionLinks'
                title:
                    type: string
        BriefOverviewSectionLinks:
            type: array
            items:
                $ref: '#/definitions/BriefOverviewSectionLink'
        BriefOverviewSectionLink:
            type: object
            properties:
                complete:
                    type: boolean
                path:
                    type: string
                    nullable: true
                text:
                    type: string
    responses:
        200:
            description: Data for the Overview page
            schema:
                $ref: '#/definitions/BriefOverview'
        400:
            description: Lot not supported.
        403:
            description: Unauthorised to view brief.
        404:
            description: brief_id not found
    """
    brief = briefs.get(brief_id)

    if not brief:
        not_found("Invalid brief id '{}'".format(brief_id))

    if current_user.role == 'buyer':
        brief_user_ids = [user.id for user in brief.users]
        if current_user.id not in brief_user_ids:
            return forbidden('Unauthorised to view brief')

    if not (brief.lot.slug == 'digital-professionals' or
            brief.lot.slug == 'training'):
        abort('Lot {} is not supported'.format(brief.lot.slug))

    sections = brief_overview_service.get_sections(brief)

    return jsonify(sections=sections, status=brief.status, title=brief.data['title']), 200


@api.route('/brief/<int:brief_id>/responses', methods=['GET'])
@login_required
def get_brief_responses(brief_id):
    """All brief responses (role=supplier,buyer)
    ---
    tags:
      - brief
    security:
      - basicAuth: []
    parameters:
      - name: brief_id
        in: path
        type: number
        required: true
    definitions:
      BriefResponses:
        properties:
          briefResponses:
            type: array
            items:
              id: BriefResponse
    responses:
      200:
        description: A list of brief responses
        schema:
          id: BriefResponses
      404:
        description: brief_id not found
    """
    brief = briefs.get(brief_id)
    if not brief:
        not_found("Invalid brief id '{}'".format(brief_id))

    if current_user.role == 'buyer':
        brief_user_ids = [user.id for user in brief.users]
        if current_user.id not in brief_user_ids:
            return forbidden("Unauthorised to view brief or brief does not exist")

    supplier_code = getattr(current_user, 'supplier_code', None)
    if current_user.role == 'supplier':
        supplier = suppliers.get_supplier_by_code(supplier_code)
        supplier_errors = SupplierValidator(supplier).validate_all()
        if len(supplier_errors) > 0:
            abort(supplier_errors)

    if current_user.role == 'buyer' and brief.status != 'closed':
        brief_responses = []
    else:
        brief_responses = brief_responses_service.get_brief_responses(brief_id, supplier_code)

    return jsonify(brief=brief.serialize(with_users=False),
                   briefResponses=brief_responses)


@api.route('/brief/<int:brief_id>/respond/documents/<string:supplier_code>/<slug>', methods=['POST'])
@login_required
def upload_brief_response_file(brief_id, supplier_code, slug):
    supplier, brief = _can_do_brief_response(brief_id)
    return jsonify({"filename": s3_upload_file_from_request(request, slug,
                                                            os.path.join(brief.framework.slug, 'documents',
                                                                         'brief-' + str(brief_id),
                                                                         'supplier-' + str(supplier.code)))
                    })


@api.route('/brief/<int:brief_id>/attachments/<slug>', methods=['POST'])
@login_required
@role_required('buyer')
def upload_brief_rfq_attachment_file(brief_id, slug):
    brief = briefs.get(brief_id)

    if not brief:
        not_found("Invalid brief id '{}'".format(brief_id))

    if current_user.role == 'buyer':
        brief_user_ids = [user.id for user in brief.users]
        if current_user.id not in brief_user_ids:
            return forbidden('Unauthorised to update brief')

    return jsonify({"filename": s3_upload_file_from_request(request, slug,
                                                            os.path.join(brief.framework.slug, 'attachments',
                                                                         'brief-' + str(brief_id)))
                    })


@api.route('/brief/<int:brief_id>/respond/documents')
@login_required
@role_required('buyer')
def download_brief_responses(brief_id):
    brief = Brief.query.filter(
        Brief.id == brief_id
    ).first_or_404()
    brief_user_ids = [user.id for user in brief.users]
    if current_user.id not in brief_user_ids:
        return forbidden("Unauthorised to view brief or brief does not exist")
    if brief.status != 'closed':
        return forbidden("You can only download documents for closed briefs")

    response = ('', 404)
    if brief.lot.slug == 'digital-professionals' or brief.lot.slug == 'training' or brief.lot.slug == 'rfx':
        try:
            file = s3_download_file(
                'brief-{}-resumes.zip'.format(brief_id),
                os.path.join(brief.framework.slug, 'archives', 'brief-{}'.format(brief_id))
            )
        except botocore.exceptions.ClientError as e:
            rollbar.report_exc_info()
            not_found("Brief documents not found for brief id '{}'".format(brief_id))

        response = Response(file, mimetype='application/zip')
        response.headers['Content-Disposition'] = 'attachment; filename="brief-{}-responses.zip"'.format(brief_id)
    elif brief.lot.slug == 'digital-outcome':
        responses = BriefResponse.query.filter(
            BriefResponse.brief_id == brief_id,
            BriefResponse.withdrawn_at.is_(None)
        ).all()
        csvdata = generate_brief_responses_csv(brief, responses)
        response = Response(csvdata, mimetype='text/csv')
        response.headers['Content-Disposition'] = (
            'attachment; filename="responses-to-requirements-{}.csv"'.format(brief_id))

    return response


@api.route('/brief/<int:brief_id>/attachments/<slug>', methods=['GET'])
@login_required
def download_brief_rfq_attachment(brief_id, slug):
    brief = Brief.query.filter(
        Brief.id == brief_id
    ).first_or_404()
    brief_user_ids = [user.id for user in brief.users]
    if hasattr(current_user, 'role') and (current_user.role == 'buyer' and current_user.id in brief_user_ids) \
            or (current_user.role == 'supplier' and
                'sellers' in brief.data and
                len(brief.data['sellers']) > 0 and
                str(current_user.supplier_code) in brief.data['sellers'].keys()):
        file = s3_download_file(slug, os.path.join(brief.framework.slug, 'attachments',
                                                   'brief-' + str(brief_id)))

        mimetype = mimetypes.guess_type(slug)[0] or 'binary/octet-stream'
        return Response(file, mimetype=mimetype)
    else:
        return forbidden("Unauthorised to view attachment or attachment does not exist")


@api.route('/brief/<int:brief_id>/respond/documents/<int:supplier_code>/<slug>', methods=['GET'])
@login_required
def download_brief_response_file(brief_id, supplier_code, slug):
    brief = Brief.query.filter(
        Brief.id == brief_id
    ).first_or_404()
    brief_user_ids = [user.id for user in brief.users]
    if hasattr(current_user, 'role') and (current_user.role == 'buyer' and current_user.id in brief_user_ids) \
            or (current_user.role == 'supplier' and current_user.supplier_code == supplier_code):
        file = s3_download_file(slug, os.path.join(brief.framework.slug, 'documents',
                                                   'brief-' + str(brief_id),
                                                   'supplier-' + str(supplier_code)))

        mimetype = mimetypes.guess_type(slug)[0] or 'binary/octet-stream'
        return Response(file, mimetype=mimetype)
    else:
        return forbidden("Unauthorised to view brief or brief does not exist")


@api.route('/brief/<int:brief_id>/respond', methods=["POST"])
@login_required
def post_brief_response(brief_id):

    brief_response_json = get_json_from_request()
    supplier, brief = _can_do_brief_response(brief_id)
    try:
        brief_response = BriefResponse(
            data=brief_response_json,
            supplier=supplier,
            brief=brief
        )

        brief_response.validate()
        db.session.add(brief_response)
        db.session.flush()

    except ValidationError as e:
        brief_response_json['brief_id'] = brief_id
        rollbar.report_exc_info(extra_data=brief_response_json)
        message = ""
        if 'essentialRequirements' in e.message and e.message['essentialRequirements'] == 'answer_required':
            message = "Essential requirements must be completed"
            del e.message['essentialRequirements']
        if 'attachedDocumentURL' in e.message:
            if e.message['attachedDocumentURL'] == 'answer_required':
                message = "Documents must be uploaded"
            if e.message['attachedDocumentURL'] == 'file_incorrect_format':
                message = "Uploaded documents are in the wrong format"
            del e.message['attachedDocumentURL']
        if len(e.message) > 0:
            message += json.dumps(e.message)
        return jsonify(message=message), 400
    except Exception as e:
        brief_response_json['brief_id'] = brief_id
        rollbar.report_exc_info(extra_data=brief_response_json)
        return jsonify(message=e.message), 400

    try:
        send_brief_response_received_email(supplier, brief, brief_response)
    except Exception as e:
        brief_response_json['brief_id'] = brief_id
        rollbar.report_exc_info(extra_data=brief_response_json)

    audit_service.log_audit_event(
        audit_type=AuditTypes.create_brief_response,
        user=current_user.email_address,
        data={
            'briefResponseId': brief_response.id,
            'briefResponseJson': brief_response_json,
        },
        db_object=brief_response)

    return jsonify(briefResponses=brief_response.serialize()), 201


@api.route('/framework/<string:framework_slug>', methods=["GET"])
def get_framework(framework_slug):
    framework = Framework.query.filter(
        Framework.slug == framework_slug
    ).first_or_404()

    return jsonify(framework.serialize())


@api.route('/brief/<int:brief_id>/assessors', methods=["GET"])
@login_required
@role_required('buyer')
@is_current_user_in_brief
def get_assessors(brief_id):
    """All brief assessors (role=buyer)
    ---
    tags:
      - brief
    security:
      - basicAuth: []
    parameters:
      - name: brief_id
        in: path
        type: number
        required: true
    definitions:
      BriefAssessors:
        type: array
        items:
          $ref: '#/definitions/BriefAssessor'
      BriefAssessor:
        type: object
        properties:
          id:
            type: integer
          brief_id:
            type: integer
          user_id:
            type: integer
          email_address:
            type: string
          user_email_address:
            type: string
    responses:
      200:
        description: A list of brief assessors
        schema:
          $ref: '#/definitions/BriefAssessors'
    """
    assessors = briefs.get_assessors(brief_id)
    return jsonify(assessors)


@api.route('/brief/<int:brief_id>/notification/<string:template>', methods=["GET"])
def get_notification_template(brief_id, template):
    brief = briefs.get(brief_id)
    if brief:
        frontend_url = current_app.config['FRONTEND_ADDRESS']
        return render_email_template(
            '{}.md'.format(template),
            frontend_url=frontend_url,
            brief_name=brief.data['title'],
            brief_id=brief.id,
            brief_url='{}/digital-marketplace/opportunities/{}'.format(frontend_url, brief_id)
        )

    return not_found('brief not found')
