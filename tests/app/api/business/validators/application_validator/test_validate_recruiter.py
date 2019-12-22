from app.api.business.validators import ApplicationValidator
from app.models import Application


def test_can_get_errors_with_none_recruiter():
    application = Application(
        data={
            'recruiter': None
        }
    )
    errors = ApplicationValidator(application).validate_recruiter()

    assert len(errors) == 1


def test_can_get_errors_with_no_recruiter():
    application = Application(
        data={
        }
    )
    errors = ApplicationValidator(application).validate_recruiter()

    assert len(errors) == 1


def test_can_get_errors_for_recruiter():
    application = Application(
        data={
            'recruiter': 'yes'
        }
    )
    errors = ApplicationValidator(application).validate_recruiter()

    assert len(errors) == 0


def test_can_get_errors_for_past_date_and_no_licence_number():
    application = Application(
        data={
            'recruiter': 'yes',
            'labourHire': {
                'vic': {
                    'expiry': '01/01/2019'
                }
            }
        }
    )
    errors = ApplicationValidator(application).validate_recruiter()

    assert len(errors) == 2


def test_can_get_error_for_no_licence_number():
    application = Application(
        data={
            'recruiter': 'yes',
            'labourHire': {
                'vic': {
                    'expiry': '01/01/2050'
                }
            }
        }
    )
    errors = ApplicationValidator(application).validate_recruiter()

    assert len(errors) == 1


def test_can_get_error_for_no_expiry():
    application = Application(
        data={
            'recruiter': 'yes',
            'labourHire': {
                'vic': {
                    'licenceNumber': 'foobar-licence'
                }
            }
        }
    )
    errors = ApplicationValidator(application).validate_recruiter()

    assert len(errors) == 1


def test_valid_for_recruiter_and_labour_hire():
    application = Application(
        data={
            'recruiter': 'yes',
            'labourHire': {
                'vic': {
                    'expiry': '01/01/2050',
                    'licenceNumber': 'foobar-licence'
                }
            }
        }
    )
    errors = ApplicationValidator(application).validate_recruiter()

    assert len(errors) == 0
