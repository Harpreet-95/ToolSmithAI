"""
PII access-policy tests — 2026-08-24 "remove standard-PII blocking for live
SQL" milestone.

Product decision: ToolSmithAI must not require any special role, ownership,
or manual approval to view ordinary business identity/contact data (names,
emails, phone numbers, addresses, ...) from a source the requester is
already authorized to query. Only the restricted category (credentials/
secrets/government-ID/payment — core.dictionary.pii_detector.
is_restricted_category) stays blocked, for every requester, unconditionally.
This replaces the prior (2026-08-22) owner/admin role-exception policy
entirely — decide_pii_access no longer takes a role/ownership parameter at
all (see that module's own docstring for why re-adding one would be wrong).

Covers core.governance.pii_access_policy.decide_pii_access directly (pure
function, no DB) plus data.query_execution_service._governance_recheck
(mocked business-context lookup, same pattern tests/test_live_query_engine.py
::TestPIIMasking already uses).
"""
import data.query_execution_service as qes
from core.governance.pii_access_policy import ALLOW, BLOCK, decide_pii_access


# ---------------------------------------------------------------------------
# decide_pii_access — pure-function matrix
# ---------------------------------------------------------------------------

class TestDecidePIIAccess:
    def test_confirmed_pii_standard_identity_allowed(self):
        d = decide_pii_access(
            table_fqn="dbo.students", column_name="Email",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == ALLOW
        assert d.policy == "pii.standard_identity_allow"

    def test_uncertain_pii_standard_identity_still_allowed(self):
        # This is the central behavior change: an ambiguous/never-confirmed
        # PII column (e.g. a bare "Name" column, Tier-3 UNCERTAIN) used to
        # BLOCK unconditionally. It must now be allowed exactly like a
        # confirmed one — classification is informational only.
        d = decide_pii_access(
            table_fqn="dbo.students", column_name="Name",
            classification="UNCERTAIN", confirmed=False,
        )
        assert d.action == ALLOW
        assert d.policy == "pii.standard_identity_allow_unclassified"

    def test_never_classified_pii_standard_identity_allowed(self):
        d = decide_pii_access(
            table_fqn="dbo.students", column_name="FirstName",
            classification=None, confirmed=False,
        )
        assert d.action == ALLOW

    def test_confirmed_non_pii_allowed(self):
        d = decide_pii_access(
            table_fqn="dbo.paths", column_name="TrackName",
            classification="CONFIRMED_NON_PII", confirmed=False,
        )
        assert d.action == ALLOW
        assert d.policy == "pii.confirmed_non_pii"

    def test_aggregated_pii_allowed(self):
        d = decide_pii_access(
            table_fqn="dbo.students", column_name="Email",
            classification="CONFIRMED_PII", confirmed=True,
            aggregation="COUNT",
        )
        assert d.action == ALLOW
        assert d.policy == "pii.aggregate_exempt"

    def test_no_role_or_ownership_parameter_exists(self):
        # decide_pii_access must not accept role/ownership at all — a
        # future caller passing them would be a signal someone tried to
        # reintroduce the removed role-based policy.
        import inspect
        sig = inspect.signature(decide_pii_access)
        assert "user_role" not in sig.parameters
        assert "is_source_owner" not in sig.parameters


class TestRestrictedCategoryAlwaysBlocked:
    """Credential/secret/government-ID/payment columns stay blocked for
    every requester, unconditionally — no override anywhere."""

    def test_ssn_allowed_not_an_authentication_secret(self):
        # 2026-09-30, Phase 7 Step 6E3B Part 6 — the owner's policy
        # explicitly excludes government-ID numbers (SSN, passport, ...)
        # and "ordinary card/account identifiers" from the authentication-
        # secret/restricted category: they remain ordinary, unmasked PII
        # for an authorized source user (still excluded from logs/caches,
        # but never unconditionally blocked). See core.dictionary.
        # pii_detector's own _RESTRICTED_TOKENS docstring for the full
        # narrowing rationale — this supersedes the pre-6E3B expectation.
        d = decide_pii_access(
            table_fqn="dbo.edu_UsersSSN", column_name="SSN",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == ALLOW
        assert d.is_restricted is False

    def test_password_blocked(self):
        d = decide_pii_access(
            table_fqn="dbo.aspnet_Membership", column_name="Password",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_password_hash_blocked(self):
        d = decide_pii_access(
            table_fqn="dbo.Students", column_name="PasswordHash",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_api_key_blocked(self):
        d = decide_pii_access(
            table_fqn="dbo.Integrations", column_name="APIKey",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_access_token_blocked(self):
        d = decide_pii_access(
            table_fqn="dbo.Users", column_name="AccessToken",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_refresh_token_blocked(self):
        d = decide_pii_access(
            table_fqn="dbo.Users", column_name="RefreshToken",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_private_key_blocked(self):
        d = decide_pii_access(
            table_fqn="dbo.Certs", column_name="PrivateKey",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_connection_string_blocked(self):
        d = decide_pii_access(
            table_fqn="dbo.DataSources", column_name="ConnectionString",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_security_answer_blocked(self):
        d = decide_pii_access(
            table_fqn="dbo.Users", column_name="SecurityAnswer",
            classification="CONFIRMED_PII", confirmed=True,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_restricted_category_blocked_even_when_confirmed_non_pii(self):
        # Belt-and-suspenders: a restricted-name column should never reach
        # ALLOW via a stale/incorrect CONFIRMED_NON_PII classification —
        # is_restricted_category is checked before the classification branch.
        d = decide_pii_access(
            table_fqn="dbo.aspnet_Membership", column_name="Password",
            classification="CONFIRMED_NON_PII", confirmed=False,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_restricted_category_blocked_even_when_unclassified(self):
        d = decide_pii_access(
            table_fqn="dbo.Users", column_name="AccessToken",
            classification=None, confirmed=False,
        )
        assert d.action == BLOCK
        assert d.is_restricted is True

    def test_restricted_category_still_allowed_when_aggregated(self):
        # COUNT(PasswordHash) never returns a raw value — matches the
        # pre-existing aggregate-exempt precedence exactly.
        d = decide_pii_access(
            table_fqn="dbo.Students", column_name="PasswordHash",
            classification="CONFIRMED_PII", confirmed=True, aggregation="COUNT",
        )
        assert d.action == ALLOW
        assert d.policy == "pii.aggregate_exempt"


# ---------------------------------------------------------------------------
# _governance_recheck — end-to-end against a mocked business context lookup,
# mirroring tests/test_live_query_engine.py::TestPIIMasking.
# ---------------------------------------------------------------------------

def _fake_ctx_batch(pii_name_heuristic=True, pii_confirmed=True, classification="CONFIRMED_PII"):
    def _fake(source_id, user_id, columns):
        return {
            (table_fqn, column_name): {
                "dictionary": {"pii_risk": True, "is_approved": True},
                "profiling": {
                    "pii_name_heuristic": pii_name_heuristic,
                    "pii_confirmed": pii_confirmed,
                    "pii_classification": classification,
                },
            }
            for table_fqn, column_name in columns
        }
    return _fake


class TestGovernanceRecheckStandardIdentity:
    def _sql_plan(self, column_name="Email"):
        return {"select": [{"table_fqn": "dbo.students", "column_name": column_name, "alias": column_name}]}

    def test_standard_user_gets_allowed_unmasked(self, monkeypatch):
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", _fake_ctx_batch())
        blocked, aliases, warnings = qes._governance_recheck(1, "28", self._sql_plan(), user_role="user")
        assert blocked == []
        assert aliases == set()  # never masked under the current policy
        assert any(w["type"] == "pii_standard_access" for w in warnings)

    def test_default_role_still_allowed(self, monkeypatch):
        # No user_role kwarg at all — every internal caller (Data Analyst
        # Agent, Executive Report, insight comparisons) that never threads a
        # role gets the SAME standard-identity access as a direct chat user.
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", _fake_ctx_batch())
        blocked, aliases, warnings = qes._governance_recheck(1, "28", self._sql_plan())
        assert blocked == []
        assert aliases == set()
        assert any(w["type"] == "pii_standard_access" for w in warnings)

    def test_uncertain_pii_allowed_not_blocked(self, monkeypatch):
        monkeypatch.setattr(
            qes, "get_column_business_contexts_batch",
            _fake_ctx_batch(pii_confirmed=False, classification="UNCERTAIN"),
        )
        blocked, aliases, warnings = qes._governance_recheck(1, "28", self._sql_plan("Name"), user_role="user")
        assert blocked == []
        assert aliases == set()
        assert any(w["type"] == "pii_standard_access" for w in warnings)

    def test_confirmed_non_pii_unaffected(self, monkeypatch):
        monkeypatch.setattr(
            qes, "get_column_business_contexts_batch",
            _fake_ctx_batch(pii_confirmed=False, classification="CONFIRMED_NON_PII"),
        )
        blocked, aliases, warnings = qes._governance_recheck(1, "28", self._sql_plan("TrackName"), user_role="user")
        assert blocked == []
        assert aliases == set()
        assert any(w["type"] == "pii_cleared" for w in warnings)

    def test_no_select_at_all_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", _fake_ctx_batch())
        blocked, aliases, warnings = qes._governance_recheck(1, "28", {}, user_role="user")
        assert blocked == [] and aliases == set() and warnings == []


class TestGovernanceRecheckRestrictedCategory:
    def test_restricted_category_blocked_for_standard_user(self, monkeypatch):
        # 2026-09-30, Phase 7 Step 6E3B Part 6 — SSN was the prior example
        # here but is no longer a restricted (unconditionally-blocked)
        # column under the narrowed policy (see TestRestrictedCategoryAlways
        # Blocked.test_ssn_allowed_not_an_authentication_secret); switched
        # to an actual authentication-secret example so this test still
        # exercises real restricted-category enforcement.
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", _fake_ctx_batch())
        sql_plan = {"select": [{"table_fqn": "dbo.Users", "column_name": "APIKey", "alias": "APIKey"}]}
        blocked, aliases, warnings = qes._governance_recheck(1, "28", sql_plan, user_role="user")
        assert blocked == ["dbo.Users.APIKey"]
        assert aliases == set()
        assert any(w["type"] == "pii_restricted" for w in warnings)

    def test_restricted_category_blocked_regardless_of_role(self, monkeypatch):
        # No exception exists for any role — this is the negative control
        # proving the removed owner/admin exception never quietly survives
        # for the one category that must stay protected.
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", _fake_ctx_batch())
        sql_plan = {"select": [{"table_fqn": "dbo.Users", "column_name": "PasswordHash", "alias": "PasswordHash"}]}
        for role in ("user", "owner", "admin"):
            blocked, aliases, warnings = qes._governance_recheck(1, "28", sql_plan, user_role=role)
            assert blocked == ["dbo.Users.PasswordHash"], f"role={role} unexpectedly allowed a restricted column"

    def test_password_field_blocked(self, monkeypatch):
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", _fake_ctx_batch())
        sql_plan = {"select": [{"table_fqn": "dbo.Students", "column_name": "PasswordHash", "alias": "PasswordHash"}]}
        blocked, aliases, warnings = qes._governance_recheck(1, "28", sql_plan)
        assert blocked == ["dbo.Students.PasswordHash"]

    def test_access_token_field_blocked(self, monkeypatch):
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", _fake_ctx_batch())
        sql_plan = {"select": [{"table_fqn": "dbo.Users", "column_name": "AccessToken", "alias": "AccessToken"}]}
        blocked, aliases, warnings = qes._governance_recheck(1, "28", sql_plan)
        assert blocked == ["dbo.Users.AccessToken"]

    def test_no_reconstruction_of_database_masked_values(self, monkeypatch):
        """ToolSmithAI never rewrites/unmasks a cell value anywhere in this
        module — _governance_recheck only ever produces an alias set that
        core.live.query_engine.LiveQueryEngine passes to data.
        query_execution_service._build_rows, which either serializes the raw
        DB-returned value untouched or replaces it with the literal '***' —
        it never inspects or alters a value the database itself returned.
        Always-empty pii_aliases under the current policy is exactly what
        guarantees this: no code path in this file ever transforms a cell
        value derived from column content."""
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", _fake_ctx_batch())
        blocked, aliases, warnings = qes._governance_recheck(
            1, "28", {"select": [{"table_fqn": "dbo.students", "column_name": "Email", "alias": "Email"}]},
        )
        assert aliases == set()


# ---------------------------------------------------------------------------
# Part 16/17 — database-native masking is passed through untouched, and no
# unmasking/reconstruction mechanism exists anywhere in the row-building path.
# ---------------------------------------------------------------------------

class TestDatabaseMaskingPreserved:
    def test_db_masked_value_returned_exactly_when_alias_not_flagged(self):
        """When pii_aliases is empty (the current policy's normal state for
        standard identity data), _build_rows must return whatever the
        database itself returned — including an already-masked value like
        '***-**-1234' — completely untouched, never re-derived."""
        description = [("SSN_Display",)]
        columns = qes._build_columns(description, pii_aliases=set())
        rows = qes._build_rows([("***-**-1234",)], columns)
        assert rows == [{"SSN_Display": "***-**-1234"}]

    def test_flagged_alias_still_replaces_with_literal_mask_never_the_real_value(self):
        """Belt-and-suspenders: if a caller ever DID pass a non-empty
        pii_aliases (nothing in the current policy does, but the mechanism
        itself must still fail safe if it were), _build_rows masks with the
        fixed literal '***' — it never derives the mask FROM the real value,
        so there is no code path that could partially leak or reconstruct it."""
        description = [("Email",)]
        columns = qes._build_columns(description, pii_aliases={"email"})
        rows = qes._build_rows([("alice@example.com",)], columns)
        assert rows == [{"Email": "***"}]

    def test_no_unmasking_helper_exists_anywhere_in_module(self):
        """Structural negative control: no function in this module's public
        or private surface claims to reverse/infer/reconstruct a masked
        value. A name like this appearing would be a sign someone added
        exactly the kind of bypass Part 26 of the milestone forbids."""
        forbidden_substrings = ("unmask", "demask", "reveal", "reconstruct", "decrypt_pii", "infer_masked")
        names = dir(qes)
        for name in names:
            lowered = name.lower()
            for forbidden in forbidden_substrings:
                assert forbidden not in lowered, f"found suspicious name {name!r} in data.query_execution_service"


class TestPIIAccessPolicySurface:
    def test_decide_pii_access_has_no_caller_identity_parameter_at_all(self):
        """Since decide_pii_access takes no role/caller-identity parameter
        (see TestDecidePIIAccess.test_no_role_or_ownership_parameter_exists
        above), it is structurally impossible for a Data Analyst Agent-
        issued question to receive a different PII outcome than an
        identical direct chat question — there is nothing left to special-
        case by caller."""
        import inspect

        from core.governance.pii_access_policy import decide_pii_access
        sig = inspect.signature(decide_pii_access)
        assert set(sig.parameters) == {"table_fqn", "column_name", "classification", "confirmed", "aggregation"}
