from datetime import timedelta
from uuid import uuid4
import unittest

from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import access_consumer
from app import main as auth
from app.access import utcnow
from app.models import AccessConsumerState, AccessEventInbox, AccessRoleAssignment, Base, User, UserLoginMethod
from app.settings import settings


class AccessTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(cls.engine)
        cls.Session = sessionmaker(bind=cls.engine, expire_on_commit=False)
        auth.publish_event = lambda *args, **kwargs: None

    def setUp(self):
        with self.Session() as db:
            db.query(AccessEventInbox).delete()
            db.query(AccessConsumerState).delete()
            db.query(AccessRoleAssignment).delete()
            db.query(UserLoginMethod).delete()
            db.query(User).delete()
            db.commit()

    def make_user(self, username: str, person_id: int | None = None, display_name: str | None = None) -> str:
        with self.Session() as db:
            user = User(
                id=str(uuid4()),
                username=username,
                password_hash=auth.password_context.hash("secret-pass"),
                person_id=person_id,
                display_name=display_name,
                scopes=["operations"],
            )
            db.add(user)
            db.commit()
            return user.id

    def grant(self, user_id: str, role_code: str, **overrides) -> str:
        with self.Session() as db:
            administrator = db.query(User).filter(User.username == "admin").one()
            values = {
                "employment_episode_id": 2201,
                "position_assignment_id": 3301,
                "membership_id": None,
                "organization_id": None,
                "valid_from": utcnow(),
                "reason": "test assignment",
            }
            values.update(overrides)
            result = auth.create_role_assignment(
                user_id,
                auth.RoleAssignmentCreate(role_code=role_code, **values),
                administrator=administrator,
                db=db,
            )
            return result["assignment_id"]

    def bootstrap_admin(self) -> str:
        admin_id = self.make_user("admin", display_name="Administrator")
        self.grant(admin_id, "administrator")
        return admin_id

    def login_claims(self, token: str) -> dict:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])

    def test_login_returns_person_roles_and_auth_version_claims(self):
        self.bootstrap_admin()
        user_id = self.make_user("operator.1042", person_id=1042, display_name="Иван Иванов")
        self.grant(user_id, "manager")

        with self.Session() as db:
            response = auth.login(auth.UserLogin(username="operator.1042", password="secret-pass"), db)

        claims = self.login_claims(response.access_token)
        self.assertEqual(claims["sub"], "operator.1042")
        self.assertEqual(claims["typ"], "user")
        self.assertEqual(claims["person_id"], "1042")
        self.assertEqual(claims["roles"], ["manager"])
        self.assertEqual(claims["auth_version"], 2)

    def test_login_by_verified_phone_uses_canonical_account(self):
        self.bootstrap_admin()
        user_id = self.make_user("operator.1042", person_id=1042)
        self.grant(user_id, "manager")
        with self.Session() as db:
            db.add(
                UserLoginMethod(
                    id=str(uuid4()),
                    user_id=user_id,
                    method_type="phone",
                    identifier="+79161234567",
                    verified_at=utcnow(),
                )
            )
            db.commit()

        with self.Session() as db:
            response = auth.login(auth.UserLogin(username="8 916 123-45-67", password="secret-pass"), db)

        claims = self.login_claims(response.access_token)
        self.assertEqual(claims["uid"], user_id)
        self.assertEqual(claims["person_id"], "1042")

    def test_verify_rejects_stale_auth_version(self):
        self.bootstrap_admin()
        user_id = self.make_user("operator.1042", person_id=1042)
        self.grant(user_id, "manager")

        with self.Session() as db:
            token = auth.login(auth.UserLogin(username="operator.1042", password="secret-pass"), db)
            claims = self.login_claims(token.access_token)
            user = db.query(User).filter(User.id == user_id).one()
            user.auth_version = int(user.auth_version) + 1
            db.commit()

            with self.assertRaises(Exception) as context:
                auth.verify_token(claims, db)
            self.assertEqual(context.exception.status_code, 401)
            self.assertEqual(context.exception.detail["code"], "token_revoked")

    def test_duplicate_active_assignment_is_rejected(self):
        self.bootstrap_admin()
        user_id = self.make_user("operator.1042", person_id=1042)
        self.grant(user_id, "manager")

        with self.Session() as db:
            administrator = db.query(User).filter(User.username == "admin").one()
            with self.assertRaises(Exception) as context:
                auth.create_role_assignment(
                    user_id,
                    auth.RoleAssignmentCreate(
                        role_code="manager",
                        employment_episode_id="2201",
                        position_assignment_id="3301",
                        valid_from=utcnow(),
                        reason="duplicate",
                    ),
                    administrator=administrator,
                    db=db,
                )
            self.assertEqual(context.exception.detail["code"], "role_assignment_already_active")

    def test_last_administrator_cannot_be_revoked(self):
        admin_id = self.bootstrap_admin()
        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            assignment = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.user_id == admin_id).one()
            with self.assertRaises(Exception) as context:
                auth.revoke_role_assignment(
                    assignment.id,
                    auth.RoleAssignmentRevoke(effective_at=utcnow(), reason="cleanup"),
                    administrator=administrator,
                    db=db,
                )
            self.assertEqual(context.exception.detail["code"], "last_administrator")

    def test_revoke_bumps_auth_version_and_keeps_history(self):
        admin_id = self.bootstrap_admin()
        user_id = self.make_user("operator.1042", person_id=1042)
        assignment_id = self.grant(user_id, "manager")

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            result = auth.revoke_role_assignment(
                assignment_id,
                auth.RoleAssignmentRevoke(effective_at=utcnow(), reason="employment ended"),
                administrator=administrator,
                db=db,
            )
            stored = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == assignment_id).one()
            owner = db.query(User).filter(User.id == user_id).one()

        self.assertEqual(result["status"], "revoked")
        self.assertEqual(stored.role_code, "manager")
        self.assertIsNotNone(stored.valid_to)
        self.assertGreaterEqual(owner.auth_version, 2)

    def test_employment_termination_revokes_only_linked_operator_roles(self):
        admin_id = self.bootstrap_admin()
        user_id = self.make_user("operator.1042", person_id=1042)
        operator_assignment = self.grant(user_id, "manager", employment_episode_id=2201, position_assignment_id=3301)
        external_assignment = self.grant(
            user_id,
            "external_user",
            employment_episode_id=None,
            position_assignment_id=None,
            membership_id=str(uuid4()),
            organization_id=str(uuid4()),
        )

        result = access_consumer.apply_access_event(
            {
                "event_id": str(uuid4()),
                "event_type": "people.employment.changed",
                "aggregate_id": "1042",
                "aggregate_version": 1,
                "payload": {
                    "person_id": "1042",
                    "employment_episode_id": "2201",
                    "employment_status": "terminated",
                    "position_assignment_id": "3301",
                    "effective_at": utcnow().isoformat(),
                },
            },
            session_factory=self.Session,
        )

        self.assertEqual(result, "applied")
        with self.Session() as db:
            operator = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == operator_assignment).one()
            external = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == external_assignment).one()
        self.assertEqual(operator.status, "revoked")
        self.assertEqual(external.status, "active")

    def test_external_membership_revocation_keeps_operator_roles(self):
        self.bootstrap_admin()
        user_id = self.make_user("external.1042", person_id=1042)
        organization_id = str(uuid4())
        operator_assignment = self.grant(user_id, "manager")
        external_assignment = self.grant(
            user_id,
            "external_user",
            employment_episode_id=None,
            position_assignment_id=None,
            membership_id=str(uuid4()),
            organization_id=organization_id,
        )

        access_consumer.apply_access_event(
            {
                "event_id": str(uuid4()),
                "event_type": "people.external_membership.changed",
                "aggregate_id": "1042",
                "aggregate_version": 1,
                "payload": {
                    "person_id": "1042",
                    "membership_id": str(uuid4()),
                    "organization_id": organization_id,
                    "membership_status": "revoked",
                    "effective_at": utcnow().isoformat(),
                },
            },
            session_factory=self.Session,
        )

        with self.Session() as db:
            operator = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == operator_assignment).one()
            external = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == external_assignment).one()
        self.assertEqual(operator.status, "active")
        self.assertEqual(external.status, "revoked")

    def test_gap_blocks_event_until_missing_version_arrives(self):
        self.bootstrap_admin()
        user_id = self.make_user("operator.1042", person_id=1042)
        payload = {"person_id": "1042", "display_name": "Иван Иванов", "name_version": 2}
        second = {
            "event_id": str(uuid4()),
            "event_type": "people.identity.changed",
            "aggregate_id": "1042",
            "aggregate_version": 2,
            "payload": payload,
        }
        self.assertEqual(access_consumer.apply_access_event(second, session_factory=self.Session), "gap")

        first = dict(second, event_id=str(uuid4()), aggregate_version=1, payload={**payload, "name_version": 1})
        self.assertEqual(access_consumer.apply_access_event(first, session_factory=self.Session), "applied")
        self.assertEqual(access_consumer.apply_access_event(second, session_factory=self.Session), "applied")
        self.assertEqual(access_consumer.apply_access_event(second, session_factory=self.Session), "duplicate")

        with self.Session() as db:
            user = db.query(User).filter(User.id == user_id).one()
        self.assertEqual(user.display_name, "Иван Иванов")
        self.assertEqual(user.name_version, 2)

    def test_merge_deactivates_duplicate_and_keeps_user_ids(self):
        self.bootstrap_admin()
        duplicate_id = self.make_user("operator.1042", person_id=1042)
        canonical_id = self.make_user("operator.2000", person_id=2000)
        self.grant(canonical_id, "manager")
        with self.Session() as db:
            db.add(
                UserLoginMethod(
                    id=str(uuid4()),
                    user_id=duplicate_id,
                    method_type="phone",
                    identifier="+79161234567",
                    verified_at=utcnow(),
                )
            )
            db.commit()

        access_consumer.apply_access_event(
            {
                "event_id": str(uuid4()),
                "event_type": "people.merged",
                "aggregate_id": "1042",
                "aggregate_version": 1,
                "payload": {
                    "duplicate_person_id": "1042",
                    "canonical_person_id": "2000",
                    "merged_at": utcnow().isoformat(),
                    "reason_code": "verified_identifier",
                },
            },
            session_factory=self.Session,
        )

        with self.Session() as db:
            duplicate = db.query(User).filter(User.id == duplicate_id).one()
            canonical = db.query(User).filter(User.id == canonical_id).one()
            methods = db.query(UserLoginMethod).filter(UserLoginMethod.user_id == canonical_id).all()

        self.assertFalse(duplicate.is_active)
        self.assertEqual(duplicate.merged_into_user_id, canonical_id)
        self.assertTrue(canonical.is_active)
        self.assertEqual(len(methods), 1)


if __name__ == "__main__":
    unittest.main()
