import unittest

from context_memory.hosted_authorization import (
    HostedAction,
    HostedResource,
    HostedSession,
    authorize_hosted_action,
)


def session(**overrides):
    values = {
        "actor_id": "user-a",
        "tenant_id": "tenant-a",
        "session_id": "session-a",
        "roles": frozenset({"project_reader"}),
        "project_ids": frozenset({"project-a"}),
    }
    values.update(overrides)
    return HostedSession(**values)


class HostedAuthorizationTests(unittest.TestCase):
    def test_requires_verified_active_session(self):
        resource = HostedResource("tenant-a", "project-a")
        self.assertEqual(
            authorize_hosted_action(None, resource, HostedAction.SEARCH).reason,
            "authentication_required",
        )
        revoked = authorize_hosted_action(
            session(active=False), resource, HostedAction.SEARCH
        )
        self.assertFalse(revoked.allowed)
        self.assertEqual(revoked.reason, "session_inactive")

    def test_rejects_cross_tenant_access_for_every_protected_action(self):
        caller = session(
            roles=frozenset({"tenant_admin"}),
            project_ids=frozenset({"project-a", "project-b"}),
        )
        for action in HostedAction:
            project_id = None if action is HostedAction.BACKUP else "project-b"
            with self.subTest(action=action):
                decision = authorize_hosted_action(
                    caller,
                    HostedResource("tenant-b", project_id),
                    action,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "tenant_mismatch")

    def test_project_grants_bound_search_export_and_event_polling(self):
        caller = session(
            roles=frozenset(
                {"project_reader", "project_exporter"}
            )
        )
        for action in (
            HostedAction.SEARCH,
            HostedAction.EXPORT,
            HostedAction.EVENT_POLL,
        ):
            with self.subTest(action=action):
                granted = authorize_hosted_action(
                    caller,
                    HostedResource("tenant-a", "project-a"),
                    action,
                )
                denied = authorize_hosted_action(
                    caller,
                    HostedResource("tenant-a", "project-b"),
                    action,
                )
                self.assertTrue(granted.allowed)
                self.assertEqual(denied.reason, "project_not_granted")

    def test_roles_are_least_privilege_and_unknown_roles_grant_nothing(self):
        reader = session()
        self.assertTrue(
            authorize_hosted_action(
                reader,
                HostedResource("tenant-a", "project-a"),
                HostedAction.SEARCH,
            ).allowed
        )
        denied = authorize_hosted_action(
            reader,
            HostedResource("tenant-a", "project-a"),
            HostedAction.EXPORT,
        )
        self.assertEqual(denied.reason, "permission_denied")
        unknown = authorize_hosted_action(
            session(roles=frozenset({"unknown"})),
            HostedResource("tenant-a", "project-a"),
            HostedAction.SEARCH,
        )
        self.assertEqual(unknown.reason, "permission_denied")

    def test_backup_requires_tenant_role_and_tenant_resource(self):
        operator = session(
            roles=frozenset({"tenant_backup_operator"}),
            project_ids=frozenset(),
        )
        self.assertTrue(
            authorize_hosted_action(
                operator,
                HostedResource("tenant-a"),
                HostedAction.BACKUP,
            ).allowed
        )
        project_backup = authorize_hosted_action(
            operator,
            HostedResource("tenant-a", "project-a"),
            HostedAction.BACKUP,
        )
        self.assertEqual(project_backup.reason, "tenant_resource_required")

    def test_incomplete_identity_context_is_rejected(self):
        decision = authorize_hosted_action(
            session(actor_id=""),
            HostedResource("tenant-a", "project-a"),
            HostedAction.SEARCH,
        )
        self.assertEqual(decision.reason, "invalid_identity_context")
