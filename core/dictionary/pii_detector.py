import re

# Single tokens that, when found in a column name, indicate PII.
_EXACT_TOKENS: frozenset[str] = frozenset({
    # Contact
    'email', 'phone', 'mobile', 'cell', 'fax', 'pager',
    # Identity documents
    'ssn', 'sin', 'nino', 'passport', 'ein', 'tin',
    # Authentication
    'password', 'passwd', 'pwd', 'passphrase', 'secret',
    'credential', 'credentials', 'pin', 'otp', 'token',
    # Payment
    'cvv', 'cvc', 'iban', 'pan',
    # Demographics
    'dob', 'gender', 'sex', 'race', 'ethnicity', 'nationality',
    # Financial
    'salary', 'wage', 'wages', 'income', 'earnings',
    # Geographic
    'latitude', 'longitude', 'lat', 'lng', 'lon',
    # 2026-08-09 stabilization — common first/last/middle-name schema
    # abbreviations. Confirmed live against a real production database: dbo.EDU_Instructor.
    # InstructorFname/InstructorLname are genuine person names, but
    # PascalCase tokenization ("InstructorFname" -> "instructor"+"fname")
    # never produces a literal "name" token, so Tier 3 below never saw them
    # — a false NEGATIVE on real PII, the opposite (and more serious)
    # failure mode than Tier 3's own bare-"name" false-positive class this
    # same stabilization pass already narrowed. Unlike bare "name"
    # (genuinely ambiguous with a business object — "path name", "company
    # name"), "fname"/"lname"/"mname" have no legitimate business-object
    # reading regardless of whatever qualifier precedes them ("Instructor",
    # "Student", "Contact", ...) — safe at Tier 1 exact-token strength, not
    # a qualifier-gated Tier 3 pattern.
    'fname', 'lname', 'mname',
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


# 2026-08-09 stabilization — Tier 3's own comment below already documented
# its false-positive class ("company_name, product_name, etc."), confirmed
# live against a real production database (dbo.EDU_Track.TrackName, dbo.EDU_Class.ClassName
# both flagged; no dictionary entry in that source has ever been approved,
# so the "reviewers clear it" escape hatch that comment assumes never
# actually ran). A generic, schema-agnostic vocabulary of person/human-
# referring qualifiers — never a specific table/column/business-entity name
# — narrows Tier 3 to fire only when "name" is either bare (no qualifier at
# all — conservative default, still fails closed) or qualified by one of
# these words. A business-object qualifier ("path", "class", "product",
# "company", ...) is not in this list and is therefore never treated as a
# PII signal by itself — but nothing here ever allowlists a specific term
# as safe; every other detection tier (email, phone, SSN, compound
# firstname/lastname/username, ...) is completely unaffected.
_PERSON_QUALIFIER_TOKENS: frozenset[str] = frozenset({
    'person', 'individual', 'human',
    'student', 'instructor', 'teacher', 'professor', 'employee', 'staff',
    'user', 'customer', 'client', 'contact', 'member', 'guest', 'patient',
    'applicant', 'candidate', 'recruiter', 'vendor', 'supplier',
    'parent', 'guardian', 'spouse', 'dependent', 'beneficiary',
    'owner', 'holder', 'author', 'creator', 'signer', 'agent',
    'representative', 'nickname', 'alias', 'signatory', 'attendee',
    'participant', 'visitor', 'subscriber',
})


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
    return detect_pii_tier(column_name, data_type)[0]


# Restricted category (2026-08-22, PII access-policy milestone; scope
# clarified 2026-08-24 — "remove standard-PII blocking for live SQL";
# NARROWED AGAIN 2026-09-30, Phase 7 Step 6E3B Part 6, per the owner's
# explicit policy: an unconditional block is reserved for actual
# system-ACCESS secrets only (something that authenticates a session or
# unlocks another system) — never a sensitive but ordinary business/
# identity value. SSN, passport numbers, and "ordinary card/account
# identifiers" (bank account/routing numbers, IBAN, PAN) are explicitly
# NOT authentication credentials under this policy and must remain
# available to an authorized source user like any other sensitive PII
# (they stay detected as PII by _EXACT_TOKENS/_PII_SUBSTRINGS above, and
# stay absent from logs/telemetry/caches per that same policy — only the
# UNCONDITIONAL-BLOCK treatment is removed). This category is the ONLY
# one core.governance.pii_access_policy / core.sqlservice.sql_validation's
# secret-column check still blocks — every other PII-flagged column
# (ordinary business identity/contact/financial/government-ID data) is
# queryable by any authorized ToolSmithAI user, unconditionally, for every
# role. Restricted-category columns stay blocked for everyone, regardless
# of role, ownership, or manual PII confirmation — there is no override
# anywhere in this codebase. Deliberately a subset of the tokens already in
# _EXACT_TOKENS/_PII_SUBSTRINGS above (every restricted column is still
# detected as PII first; this only narrows which PII gets the stricter
# treatment), so no new false-positive surface is introduced.
_RESTRICTED_TOKENS: frozenset[str] = frozenset({
    'password', 'passwd', 'pwd', 'passphrase', 'secret',
    'credential', 'credentials', 'pin', 'otp', 'token',
    'cvv', 'cvc',
})

_RESTRICTED_SUBSTRINGS: tuple[str, ...] = (
    # 2026-09-30, Phase 7 Step 6E3B Part 6 — 'socialsecurity'/'nationalid'/
    # 'taxid'/'taxnumber'/'creditcard'/'cardnumber'/'cardnum'/'ccnumber'/
    # 'bankaccount'/'bankacct'/'accountnumber'/'routingnumber' were REMOVED
    # from here: the owner's policy explicitly excludes government-ID and
    # "ordinary card/account identifiers" from the authentication-secret
    # definition — they remain ordinary (unmasked, but log/cache-excluded)
    # PII, not an unconditional block. 'drivinglicense'/'driverslicense'
    # are intentionally still restricted (a physical-credential identity
    # document, not named in the owner's "remains available" list).
    'drivinglicense', 'driverslicense',
    'password', 'passwd', 'passphrase',
    'apikey', 'secretkey', 'privatekey', 'accesskey',
    'authtoken', 'accesstoken', 'refreshtoken', 'bearertoken', 'jwttoken',
    # 2026-08-24 "remove standard-PII blocking" milestone, Part 4 — the
    # restricted category becomes the ONLY thing that still gates access
    # (see core.governance.pii_access_policy), so its own coverage must be
    # complete for every credential/secret shape the milestone explicitly
    # names, not just the shapes the prior owner/admin-exception milestone
    # happened to need. connectionstring/securityanswer/securityquestion
    # were not previously covered by any token or substring here.
    'connectionstring', 'connstring', 'connstr',
    'securityanswer', 'securityquestion',
    # Phase 7, Step 6E3B, Part 6 gap fix — 'apikey'/'secretkey'/'privatekey'/
    # 'accesskey' were already covered as compounds, but a column literally
    # named EncryptionKey/MasterKey matched no token or substring here (a
    # bare 'key' token is deliberately never added, to avoid flagging
    # ordinary PrimaryKey/ForeignKey business columns).
    'encryptionkey', 'masterkey',
)

# Bump whenever _RESTRICTED_TOKENS/_RESTRICTED_SUBSTRINGS changes in a way
# that could change a prior is_restricted_category() result for an
# unchanged column name — read by data.view_profile_service/core.
# sqlservice.fast_path_plan_cache as one component of the validated-plan
# cache key (Phase 7, Step 6E3B, Part 10), so any widening/narrowing of
# secret-column coverage invalidates every previously cached plan instead
# of silently reusing one validated under a stale policy.
SECRET_POLICY_VERSION: int = 1


def is_restricted_category(column_name: str) -> bool:
    """True for credential/government-ID/payment-instrument columns that
    must stay blocked even for an otherwise-authorized owner/admin (Part 8
    of the PII access-policy milestone). Name-pattern heuristic only, same
    conservative approach as detect_pii_tier — never accesses data values."""
    if not column_name:
        return False
    tokens = _tokenize(column_name)
    collapsed = ''.join(tokens)
    if _RESTRICTED_TOKENS.intersection(tokens):
        return True
    return any(substr in collapsed for substr in _RESTRICTED_SUBSTRINGS)


def detect_pii_tier(column_name: str, data_type: str) -> tuple[bool, int, str]:
    """Same detection as detect_pii(), plus which tier fired and why.

    Returns (is_pii_heuristic, tier, evidence). tier is 0 when no tier fired
    (is_pii_heuristic False). Tiers 1 and 2 are unambiguous name/type
    patterns (email, ssn, firstname, ...) — high-confidence signal, safe for
    automatic PII confirmation. Tier 3 ('name' token, bare or qualified by a
    person-referring word) is a deliberately conservative catch-all that
    still produces real false positives on business-object columns despite
    the qualifier narrowing — used by data/pii_classification_service.py to
    keep Tier-3-only hits at UNCERTAIN rather than auto-confirming them.
    """
    if not column_name:
        return False, 0, "empty column name"

    tokens = _tokenize(column_name)
    collapsed = ''.join(tokens)

    # Tier 1: any single token is an unambiguous PII indicator
    hit = _EXACT_TOKENS.intersection(tokens)
    if hit:
        return True, 1, f"exact PII token match: {sorted(hit)[0]}"

    # Tier 2: collapsed name contains a compound PII pattern
    for substr in _PII_SUBSTRINGS:
        if substr in collapsed:
            return True, 2, f"compound PII pattern match: {substr}"

    # Tier 3: 'name' token in a TEXT column — likely a person/user name
    # field, but ONLY when bare (no qualifier — e.g. a column literally
    # called "Name", where there's no context to say otherwise, so this
    # still fails closed) or when the qualifier immediately before "name"
    # is itself a generic person/human-referring word (see
    # _PERSON_QUALIFIER_TOKENS above — e.g. "StudentName", "CustomerName").
    # A business-object qualifier ("TrackName", "ClassName", "ProductName",
    # "CompanyName", ...) is deliberately NOT flagged by this tier alone —
    # narrowed from the prior unconditional "any '...Name' column" rule,
    # which this same comment used to document as producing exactly that
    # false-positive class. Every other tier (email/phone/SSN/compound
    # firstname-lastname-username/etc.) is unchanged.
    if 'name' in tokens and data_type == 'TEXT':
        idx = tokens.index('name')
        qualifier = tokens[idx - 1] if idx > 0 else None
        if qualifier is None:
            return True, 3, "bare 'name' column, no qualifier"
        if qualifier in _PERSON_QUALIFIER_TOKENS:
            return True, 3, f"person-qualified name column: {qualifier}Name"

    return False, 0, "no PII pattern matched"
