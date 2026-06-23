import re

# Single tokens that, when found in a column name, indicate PII.
_EXACT_TOKENS: frozenset[str] = frozenset({
    # Contact
    'email', 'phone', 'mobile', 'cell', 'fax', 'pager',
    # Identity documents
    'ssn', 'sin', 'nino', 'passport', 'ein', 'tin',
    # Authentication
    'password', 'passwd', 'pwd', 'passphrase', 'secret',
    'credential', 'credentials', 'pin', 'otp',
    # Payment
    'cvv', 'cvc', 'iban', 'pan',
    # Demographics
    'dob', 'gender', 'sex', 'race', 'ethnicity', 'nationality',
    # Financial
    'salary', 'wage', 'wages', 'income', 'earnings',
    # Geographic
    'latitude', 'longitude', 'lat', 'lng', 'lon',
})

# Substrings checked against the collapsed (separator-free) lowercase column name.
# Ordered from most specific to least to short-circuit early.
_PII_SUBSTRINGS: tuple[str, ...] = (
    # Contact
    'email', 'phone', 'mobile', 'cellphone',
    # Address
    'address', 'streetaddr', 'zipcode', 'postalcode', 'postcode',
    # Identity documents
    'passport', 'socialsecurity', 'nationalid',
    'drivinglicense', 'driverslicense',
    'taxid', 'taxnumber',
    # Authentication / credentials
    'password', 'passwd', 'passphrase',
    'apikey', 'secretkey', 'privatekey', 'accesskey',
    'authtoken', 'accesstoken', 'refreshtoken', 'bearertoken', 'jwttoken',
    # Payment
    'creditcard', 'cardnumber', 'cardnum', 'ccnumber',
    'bankaccount', 'bankacct', 'accountnumber', 'routingnumber',
    # Network identifiers
    'ipaddress', 'ipaddr', 'macaddress', 'macaddr',
    # Geographic
    'geolocation', 'gpscoord', 'geocoord',
    # Personal names (compound forms caught before the Tier 3 name check)
    'firstname', 'lastname', 'fullname', 'middlename', 'givenname',
    'familyname', 'maidenname', 'surname', 'username',
    # Birth
    'birthdate', 'dateofbirth', 'birthday', 'birthyear', 'birthmonth',
    # Financial
    'salary', 'income', 'wage', 'earnings', 'compensation', 'payrate', 'payroll',
    # Health / insurance (conservative)
    'healthid', 'medicalid', 'insuranceid',
)


def _tokenize(column_name: str) -> list[str]:
    # Split PascalCase / camelCase before splitting on separators.
    # 'FirstName'  → 'First Name'
    # 'APIKey'     → 'API Key'
    # 'api_key'    → ['api', 'key'] (handled by the re.split below)
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', column_name)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', s)
    return [t.lower() for t in re.split(r'[^a-zA-Z0-9]+', s) if t]


def detect_pii(column_name: str, data_type: str) -> bool:
    """Return True if the column is likely to contain personally identifiable information.

    Detection is purely heuristic — column name patterns and data type only.
    No data values are accessed. Conservative by design: false positives are
    expected and resolved during human dictionary review.
    """
    if not column_name:
        return False

    tokens = _tokenize(column_name)
    collapsed = ''.join(tokens)

    # Tier 1: any single token is an unambiguous PII indicator
    if _EXACT_TOKENS.intersection(tokens):
        return True

    # Tier 2: collapsed name contains a compound PII pattern
    for substr in _PII_SUBSTRINGS:
        if substr in collapsed:
            return True

    # Tier 3: 'name' token in a TEXT column — likely a person/user name field.
    # Produces false positives for company_name, product_name, etc.
    # Reviewers clear those during dictionary approval.
    if 'name' in tokens and data_type == 'TEXT':
        return True

    return False
