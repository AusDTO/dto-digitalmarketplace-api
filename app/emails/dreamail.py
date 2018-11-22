from flask import current_app
from .util import render_email_template, send_or_handle_error, fill_template


def send_dreamail():
    from app.api.services import (
        audit_service,
        audit_types,
        suppliers
    )

    result = suppliers.get_suppliers_with_rejected_price()

    for item in result:
        supplier_code = item['code']
        supplier = suppliers.find(code=item['code']).one_or_none()

        sent = audit_service.find(
            object_id=supplier.id,
            object_type='Supplier',
            type='seller_to_review_pricing_case_study_email'
        ).one_or_none()
        if sent:
            continue

        case_studies = supplier.case_studies
        domains = supplier.domains

        option_1_aoe = []
        option_2_aoe = []
        for domain in domains:
            domain_name = domain.domain.name
            case_studies_in_domain = [cs for cs in case_studies if cs.data['service'] == domain_name]
            no_approved_case_studies = all(cs.status == 'rejected' for cs in case_studies_in_domain)
            if no_approved_case_studies:
                option_2_aoe.append('* {}'.format(domain_name))
            else:
                option_1_aoe.append('* {}'.format(domain_name))

        dreamail_option_1_content = ''
        if option_1_aoe:
            dreamail_option_1_content = fill_template(
                'dreamail_option_1.md',
                aoe='\n'.join(option_1_aoe)
            )

        dreamail_option_2_content = ''
        if option_2_aoe:
            dreamail_option_2_content = fill_template(
                'dreamail_option_2.md',
                frontend_url=current_app.config['FRONTEND_ADDRESS'],
                aoe='\n'.join(option_2_aoe)
            )

        email_body = render_email_template(
            'dreamail.md',
            dreamail_option_1=dreamail_option_1_content,
            dreamail_option_2=dreamail_option_2_content,
            frontend_url=current_app.config['FRONTEND_ADDRESS'],
            supplier_name=supplier.name
        )

        subject = 'Please review your pricing and/or case studies on the Marketplace'

        to_addresses = [
            e['email_address']
            for e in suppliers.get_supplier_contacts(supplier_code)
        ]

        send_or_handle_error(
            to_addresses,
            email_body,
            subject,
            current_app.config['DM_GENERIC_NOREPLY_EMAIL'],
            current_app.config['DM_GENERIC_SUPPORT_NAME'],
            event_description_for_errors=audit_types.seller_to_review_pricing_case_study_email
        )

        audit_service.log_audit_event(
            audit_type=audit_types.seller_to_review_pricing_case_study_email,
            user='',
            data={
                "to_addresses": ', '.join(to_addresses),
                "email_body": email_body,
                "subject": subject
            },
            db_object=supplier)
