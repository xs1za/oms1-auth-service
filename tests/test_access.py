from datetime import timedelta
from uuid import uuid4
import unittest

from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import access_consumer
from app import main as auth
from app import seed as auth_seed
from app.access import utcnow
from app.models import (
    AccessConsumerState,
    AccessEventInbox,
    AccessRoleAssignment,
    Base,
    ExternalOrganizationMembership,
    OperatorEmploymentPeriod,
    OperatorPosition,
    OperatorPositionAssignment,
    ServiceClient,
    User,
    UserLoginMethod,
)
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
            db.query(OperatorPositionAssignment).delete()
            db.query(ExternalOrganizationMembership).delete()
            db.query(OperatorEmploymentPeriod).delete()
            db.query(UserLoginMethod).delete()
            db.query(User).delete()
            db.query(ServiceClient).delete()
            db.query(OperatorPosition).delete()
            db.add(OperatorPosition(code="operator_software", name="Оператор ПО", is_active=True))
            db.commit()

    def make_user(
        self,
        username: str,
        person_id: int | None = None,
        display_name: str | None = None,
        is_active: bool = True,
    ) -> str:
        with self.Session() as db:
            user = User(
                id=str(uuid4()),
                username=username,
                password_hash=auth.password_context.hash("secret-pass"),
                person_id=person_id,
                display_name=display_name,
                is_active=is_active,
                scopes=["operations"],
            )
            db.add(user)
            db.commit()
            return user.id

    def add_period(
        self,
        user_id: str,
        personnel_number: str | None = None,
        starts_at=None,
    ) -> tuple[str, str]:
        moment = starts_at or (utcnow() - timedelta(days=1))
        with self.Session() as db:
            period = OperatorEmploymentPeriod(
                id=str(uuid4()),
                user_id=user_id,
                personnel_number=personnel_number,
                status="active",
                starts_at=moment,
                source="hr",
            )
            assignment = OperatorPositionAssignment(
                id=str(uuid4()),
                employment_period_id=period.id,
                position_code="operator_software",
                status="active",
                starts_at=moment,
            )
            db.add(period)
            db.add(assignment)
            db.commit()
            return period.id, assignment.id

    def add_membership(self, user_id: str, organization_id: str | None = None):
        organization = organization_id or str(uuid4())
        with self.Session() as db:
            membership = ExternalOrganizationMembership(
                id=str(uuid4()),
                user_id=user_id,
                organization_id=organization,
                status="active",
                valid_from=utcnow() - timedelta(days=1),
            )
            db.add(membership)
            db.commit()
            return membership.id, organization

    def grant(
        self,
        user_id: str,
        role_code: str,
        period_id: str | None = None,
        position_id: str | None = None,
        membership_id: str | None = None,
        **overrides,
    ) -> str:
        if role_code != "external_user" and (period_id is None or position_id is None):
            period_id, position_id = self.add_period(user_id)
        with self.Session() as db:
            administrator = db.query(User).filter(User.username == "admin").one()
            values = {
                "operator_employment_period_id": period_id if role_code != "external_user" else None,
                "operator_position_assignment_id": position_id if role_code != "external_user" else None,
                "membership_id": membership_id if role_code == "external_user" else None,
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

    def bootstrap_admin(self) -> tuple[str, str, str]:
        admin_id = self.make_user("admin", display_name="Administrator")
        period_id, position_id = self.add_period(admin_id, personnel_number="ADMIN-001")
        self.grant(admin_id, "administrator", period_id, position_id)
        return admin_id, period_id, position_id

    def login_claims(self, token: str) -> dict:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])

    def login(self, username: str) -> dict:
        with self.Session() as db:
            response = auth.login(auth.UserLogin(username=username, password="secret-pass"), db)
        return self.login_claims(response.access_token)

    def verify(self, claims: dict) -> dict:
        with self.Session() as db:
            return auth.verify_token(claims, db)

    def test_seed_creates_admin_period_position_and_linked_administrator_role(self):
        settings.admin_initial_password = "admin-test-password"
        settings.oms2_service_secret = "oms2-secret"
        settings.oms3_service_secret = "oms3-secret"
        settings.oms4_service_secret = "oms4-secret"
        settings.oms5_service_secret = "oms5-secret"
        auth_seed.SERVICE_CLIENTS = {
            "OMS2": ("oms2-secret", ["auth.verify", "employees"]),
            "OMS3": ("oms3-secret", ["auth.verify", "reports"]),
            "OMS4": ("oms4-secret", ["auth.verify", "notifications"]),
            "OMS5": ("oms5-secret", ["auth.verify", "operations"]),
        }

        with self.Session() as db:
            first = auth_seed.seed_auth_data(db)
            second = auth_seed.seed_auth_data(db)
            admin = db.query(User).filter(User.username == "admin").one()
            period = db.query(OperatorEmploymentPeriod).filter(
                OperatorEmploymentPeriod.user_id == admin.id,
                OperatorEmploymentPeriod.status == "active",
            ).one()
            assignment = db.query(OperatorPositionAssignment).filter(
                OperatorPositionAssignment.employment_period_id == period.id,
                OperatorPositionAssignment.status == "active",
            ).one()
            role = db.query(AccessRoleAssignment).filter(
                AccessRoleAssignment.user_id == admin.id,
                AccessRoleAssignment.role_code == "administrator",
                AccessRoleAssignment.status == "active",
            ).one()
            periods = db.query(OperatorEmploymentPeriod).count()
            positions = db.query(OperatorPositionAssignment).count()

        self.assertEqual(first["users"], 1)
        self.assertEqual(second["users"], 0)
        self.assertEqual(second["service_clients"], 0)
        self.assertEqual(period.personnel_number, "ADMIN-001")
        self.assertEqual(assignment.position_code, "operator_software")
        self.assertEqual(role.operator_employment_period_id, period.id)
        self.assertEqual(role.operator_position_assignment_id, assignment.id)
        self.assertEqual(periods, 1)
        self.assertEqual(positions, 1)

    def test_login_returns_person_roles_and_auth_version_claims(self):
        self.bootstrap_admin()
        user_id = self.make_user("operator.1042", person_id=1042, display_name="Иван Иванов")
        period_id, position_id = self.add_period(user_id)
        self.grant(user_id, "manager", period_id, position_id)

        claims = self.login("operator.1042")

        self.assertEqual(claims["sub"], "operator.1042")
        self.assertEqual(claims["typ"], "user")
        self.assertEqual(claims["person_id"], "1042")
        self.assertEqual(claims["roles"], ["manager"])
        self.assertEqual(claims["auth_version"], 2)
        verified = self.verify(claims)
        self.assertEqual(verified["person_id"], "1042")
        self.assertIsNone(verified["organization_id"])

    def test_operator_without_performer_profile_logs_in_with_null_person_id(self):
        self.bootstrap_admin()
        user_id = self.make_user("operator.staff")
        period_id, position_id = self.add_period(user_id)
        self.grant(user_id, "manager", period_id, position_id)

        claims = self.login("operator.staff")

        self.assertIsNone(claims.get("person_id"))
        verified = self.verify(claims)
        self.assertIsNone(verified["person_id"])
        self.assertEqual(verified["roles"], ["manager"])

    def test_create_period_creates_active_position_and_rejects_second_period(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("operator.new")
        starts_at = (utcnow() - timedelta(days=1)).isoformat().replace("+00:00", "Z")

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            response = auth.create_operator_employment_period(
                user_id,
                auth.OperatorEmploymentPeriodCreate(
                    starts_at=starts_at,
                    position_code="operator_software",
                    personnel_number="QA-OPS-001",
                    reason="Приём на должность оператора ПО",
                ),
                administrator=administrator,
                db=db,
            )

            with self.assertRaises(Exception) as context:
                auth.create_operator_employment_period(
                    user_id,
                    auth.OperatorEmploymentPeriodCreate(
                        starts_at=starts_at,
                        position_code="operator_software",
                        personnel_number="QA-OPS-002",
                        reason="Повторный приём",
                    ),
                    administrator=administrator,
                    db=db,
                )

            position = db.query(OperatorPositionAssignment).filter(
                OperatorPositionAssignment.employment_period_id
                == response["operator_employment_period_id"]
            ).one()

        self.assertEqual(response["status"], "active")
        self.assertEqual(response["personnel_number"], "QA-OPS-001")
        self.assertEqual(response["position_assignment"]["position_code"], "operator_software")
        self.assertEqual(response["position_assignment"]["status"], "active")
        self.assertEqual(position.status, "active")
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(context.exception.detail["code"], "operator_employment_already_active")

    def test_create_period_validates_position_and_date_range(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("operator.invalid")

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            with self.assertRaises(Exception) as position_error:
                auth.create_operator_employment_period(
                    user_id,
                    auth.OperatorEmploymentPeriodCreate(
                        starts_at=utcnow(),
                        position_code="unknown_position",
                        reason="Bad position",
                    ),
                    administrator=administrator,
                    db=db,
                )
            self.assertEqual(position_error.exception.detail["code"], "invalid_position_code")

            with self.assertRaises(Exception) as period_error:
                auth.create_operator_employment_period(
                    user_id,
                    auth.OperatorEmploymentPeriodCreate(
                        starts_at=utcnow() - timedelta(days=100),
                        position_code="operator_software",
                        reason="Bad period",
                    ),
                    administrator=administrator,
                    db=db,
                )
            self.assertEqual(period_error.exception.detail["code"], "invalid_period")

    def test_termination_revokes_only_period_roles_and_invalidates_old_token(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("operator.term")
        period_id, position_id = self.add_period(user_id, personnel_number="QA-TERM-001")
        self.grant(user_id, "manager", period_id, position_id)
        other_id = self.make_user("operator.other")
        other_period, other_position = self.add_period(other_id)
        self.grant(other_id, "supervisor", other_period, other_position)

        claims = self.login("operator.term")
        with self.Session() as db:
            before = db.query(User).filter(User.id == user_id).one().auth_version

        effective_at = (utcnow() + timedelta(days=1))
        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            response = auth.terminate_operator_employment_period(
                period_id,
                auth.OperatorEmploymentPeriodTerminate(
                    effective_at=effective_at,
                    reason="Увольнение",
                ),
                administrator=administrator,
                db=db,
            )
            manager = db.query(AccessRoleAssignment).filter(
                AccessRoleAssignment.operator_employment_period_id == period_id
            ).one()
            supervisor = db.query(AccessRoleAssignment).filter(
                AccessRoleAssignment.operator_employment_period_id == other_period
            ).one()
            after = db.query(User).filter(User.id == user_id).one().auth_version
            assignment = db.query(OperatorPositionAssignment).filter(
                OperatorPositionAssignment.id == position_id
            ).one()

        self.assertEqual(response["status"], "terminated")
        self.assertEqual(response["revoked_role_assignments"], 1)
        self.assertEqual(manager.status, "revoked")
        self.assertEqual(assignment.status, "terminated")
        self.assertEqual(supervisor.status, "active")
        self.assertEqual(after, before + 1)

        with self.assertRaises(Exception) as context:
            self.verify(claims)
        self.assertEqual(context.exception.status_code, 401)
        self.assertEqual(context.exception.detail["code"], "token_revoked")

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            repeat = auth.terminate_operator_employment_period(
                period_id,
                auth.OperatorEmploymentPeriodTerminate(
                    effective_at=effective_at,
                    reason="Увольнение",
                ),
                administrator=administrator,
                db=db,
            )
            unchanged = db.query(User).filter(User.id == user_id).one().auth_version
            with self.assertRaises(Exception) as conflict:
                auth.terminate_operator_employment_period(
                    period_id,
                    auth.OperatorEmploymentPeriodTerminate(
                        effective_at=effective_at + timedelta(days=1),
                        reason="Другая причина",
                    ),
                    administrator=administrator,
                    db=db,
                )

        self.assertEqual(repeat["revoked_role_assignments"], 0)
        self.assertEqual(unchanged, after)
        self.assertEqual(conflict.exception.detail["code"], "operator_employment_period_conflict")

    def test_external_membership_derives_organization_and_revokes_only_external_role(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("external.user")
        operator_period, operator_position = self.add_period(user_id)
        self.grant(user_id, "manager", operator_period, operator_position)

        valid_from = (utcnow() - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            membership = auth.create_external_membership(
                user_id,
                auth.ExternalMembershipCreate(
                    organization_id="3f6e2890-f24e-48e3-9034-c81f64716ebd",
                    valid_from=valid_from,
                ),
                administrator=administrator,
                db=db,
            )
            with self.assertRaises(Exception) as duplicate:
                auth.create_external_membership(
                    user_id,
                    auth.ExternalMembershipCreate(
                        organization_id="3f6e2890-f24e-48e3-9034-c81f64716ebd",
                        valid_from=valid_from,
                    ),
                    administrator=administrator,
                    db=db,
                )

        self.assertEqual(membership["organization_id"], "3f6e2890-f24e-48e3-9034-c81f64716ebd")
        self.assertEqual(duplicate.exception.detail["code"], "external_membership_already_active")

        assignment_id = self.grant(user_id, "external_user", membership_id=membership["membership_id"])
        with self.Session() as db:
            stored = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == assignment_id).one()
            self.assertEqual(stored.operator_employment_period_id, None)
            self.assertEqual(stored.membership_id, membership["membership_id"])

        claims = self.login("external.user")
        self.assertEqual(claims["organization_id"], "3f6e2890-f24e-48e3-9034-c81f64716ebd")
        self.assertIn("shifts:publish", claims["scopes"])
        self.assertIn("assignment_stats:read", claims["scopes"])
        verified = self.verify(claims)
        self.assertEqual(verified["organization_id"], "3f6e2890-f24e-48e3-9034-c81f64716ebd")
        self.assertEqual(verified["roles"], ["external_user", "manager"])

        effective_at = utcnow() + timedelta(days=1)
        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            response = auth.revoke_external_membership(
                membership["membership_id"],
                auth.ExternalMembershipRevoke(effective_at=effective_at, reason="Сотрудничество завершено"),
                administrator=administrator,
                db=db,
            )
            external = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == assignment_id).one()
            operator = db.query(AccessRoleAssignment).filter(
                AccessRoleAssignment.operator_employment_period_id == operator_period
            ).one()

        self.assertEqual(response["status"], "revoked")
        self.assertEqual(response["revoked_role_assignments"], 1)
        self.assertEqual(external.status, "revoked")
        self.assertEqual(operator.status, "active")

        with self.assertRaises(Exception) as context:
            self.verify(claims)
        self.assertEqual(context.exception.detail["code"], "token_revoked")

    def test_external_role_rejects_client_supplied_organization_id(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("external.invalid")
        membership_id, organization_id = self.add_membership(user_id)

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            with self.assertRaises(Exception) as context:
                auth.create_role_assignment(
                    user_id,
                    auth.RoleAssignmentCreate(
                        role_code="external_user",
                        membership_id=membership_id,
                        organization_id=organization_id,
                        valid_from=utcnow(),
                        reason="Доступ",
                    ),
                    administrator=administrator,
                    db=db,
                )
            self.assertEqual(context.exception.status_code, 422)
            self.assertEqual(context.exception.detail["code"], "invalid_role_context")

            with self.assertRaises(Exception) as legacy:
                auth.create_role_assignment(
                    user_id,
                    auth.RoleAssignmentCreate(
                        role_code="manager",
                        employment_episode_id="2201",
                        operator_employment_period_id="ignored",
                        operator_position_assignment_id="ignored",
                        valid_from=utcnow(),
                        reason="Legacy",
                    ),
                    administrator=administrator,
                    db=db,
                )
            self.assertEqual(legacy.exception.detail["code"], "invalid_role_context")

    def test_role_assignment_validates_operator_context_and_duplicates(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("operator.ctx")
        period_id, position_id = self.add_period(user_id)

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            with self.assertRaises(Exception) as missing:
                auth.create_role_assignment(
                    user_id,
                    auth.RoleAssignmentCreate(
                        role_code="manager",
                        valid_from=utcnow(),
                        reason="No context",
                    ),
                    administrator=administrator,
                    db=db,
                )
            self.assertEqual(missing.exception.detail["code"], "invalid_role_context")

            response = auth.create_role_assignment(
                user_id,
                auth.RoleAssignmentCreate(
                    role_code="manager",
                    operator_employment_period_id=period_id,
                    operator_position_assignment_id=position_id,
                    valid_from=utcnow(),
                    reason="Назначение по приказу",
                ),
                administrator=administrator,
                db=db,
            )
            with self.assertRaises(Exception) as duplicate:
                auth.create_role_assignment(
                    user_id,
                    auth.RoleAssignmentCreate(
                        role_code="manager",
                        operator_employment_period_id=period_id,
                        operator_position_assignment_id=position_id,
                        valid_from=utcnow(),
                        reason="Повтор",
                    ),
                    administrator=administrator,
                    db=db,
                )

        self.assertEqual(response["operator_employment_period_id"], period_id)
        self.assertEqual(response["operator_position_assignment_id"], position_id)
        self.assertIsNone(response["organization_id"])
        self.assertEqual(duplicate.exception.detail["code"], "role_assignment_already_active")

        other_id = self.make_user("operator.ctx2")
        other_period, other_position = self.add_period(other_id)
        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            with self.assertRaises(Exception) as foreign:
                auth.create_role_assignment(
                    user_id,
                    auth.RoleAssignmentCreate(
                        role_code="supervisor",
                        operator_employment_period_id=other_period,
                        operator_position_assignment_id=other_position,
                        valid_from=utcnow(),
                        reason="Wrong owner",
                    ),
                    administrator=administrator,
                    db=db,
                )
        self.assertEqual(foreign.exception.detail["code"], "invalid_role_context")

    def test_role_context_must_be_active_at_valid_from(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("operator.inactive")
        period_id, position_id = self.add_period(user_id)
        with self.Session() as db:
            period = db.query(OperatorEmploymentPeriod).filter(OperatorEmploymentPeriod.id == period_id).one()
            period.status = "terminated"
            position = db.query(OperatorPositionAssignment).filter(
                OperatorPositionAssignment.id == position_id
            ).one()
            position.status = "terminated"
            db.commit()

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            with self.assertRaises(Exception) as context:
                auth.create_role_assignment(
                    user_id,
                    auth.RoleAssignmentCreate(
                        role_code="manager",
                        operator_employment_period_id=period_id,
                        operator_position_assignment_id=position_id,
                        valid_from=utcnow(),
                        reason="Inactive context",
                    ),
                    administrator=administrator,
                    db=db,
                )
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(context.exception.detail["code"], "role_context_not_active")

    def test_last_administrator_cannot_be_revoked(self):
        admin_id, _, _ = self.bootstrap_admin()
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
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("operator.revoke")
        period_id, position_id = self.add_period(user_id)
        assignment_id = self.grant(user_id, "manager", period_id, position_id)

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
        self.assertGreaterEqual(owner.auth_version, 3)

    def test_performer_link_unlink_and_person_conflict(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("operator.person")
        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            linked = auth.set_performer_link(
                user_id,
                auth.PerformerLinkUpdate(person_id="1042"),
                administrator=administrator,
                db=db,
            )
        self.assertEqual(linked["person_id"], "1042")

        claims = self.login("operator.person")
        self.assertEqual(claims["person_id"], "1042")
        self.assertEqual(self.verify(claims)["person_id"], "1042")

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            unlinked = auth.set_performer_link(
                user_id,
                auth.PerformerLinkUpdate(person_id=None),
                administrator=administrator,
                db=db,
            )
        self.assertIsNone(unlinked["person_id"])

        new_claims = self.login("operator.person")
        self.assertIsNone(new_claims.get("person_id"))
        self.assertIsNone(self.verify(new_claims)["person_id"])

        second_id = self.make_user("operator.person2")
        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            auth.set_performer_link(
                user_id,
                auth.PerformerLinkUpdate(person_id="2000"),
                administrator=administrator,
                db=db,
            )
            with self.assertRaises(Exception) as conflict:
                auth.set_performer_link(
                    second_id,
                    auth.PerformerLinkUpdate(person_id="2000"),
                    administrator=administrator,
                    db=db,
                )
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(conflict.exception.detail["code"], "person_account_conflict")

        with self.Session() as db:
            administrator = db.query(User).filter(User.id == admin_id).one()
            with self.assertRaises(Exception) as invalid:
                auth.set_performer_link(
                    second_id,
                    auth.PerformerLinkUpdate(person_id="not-a-number"),
                    administrator=administrator,
                    db=db,
                )
        self.assertEqual(invalid.exception.detail["code"], "validation_error")

    def test_employment_and_membership_events_are_recorded_without_revoking_roles(self):
        admin_id, _, _ = self.bootstrap_admin()
        user_id = self.make_user("operator.event", person_id=1042)
        period_id, position_id = self.add_period(user_id)
        operator_assignment = self.grant(user_id, "manager", period_id, position_id)
        membership_id, organization_id = self.add_membership(user_id)
        external_assignment = self.grant(user_id, "external_user", membership_id=membership_id)

        employment_event_id = str(uuid4())
        membership_event_id = str(uuid4())
        self.assertEqual(
            access_consumer.apply_access_event(
                {
                    "event_id": employment_event_id,
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
            ),
            "applied",
        )
        self.assertEqual(
            access_consumer.apply_access_event(
                {
                    "event_id": membership_event_id,
                    "event_type": "people.external_membership.changed",
                    "aggregate_id": "1042",
                    "aggregate_version": 2,
                    "payload": {
                        "person_id": "1042",
                        "membership_id": membership_id,
                        "organization_id": organization_id,
                        "membership_status": "revoked",
                        "effective_at": utcnow().isoformat(),
                    },
                },
                session_factory=self.Session,
            ),
            "applied",
        )

        with self.Session() as db:
            operator = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == operator_assignment).one()
            external = db.query(AccessRoleAssignment).filter(AccessRoleAssignment.id == external_assignment).one()
            inbox = db.query(AccessEventInbox).all()

        self.assertEqual(operator.status, "active")
        self.assertEqual(external.status, "active")
        self.assertEqual({event.event_id for event in inbox}, {employment_event_id, membership_event_id})

    def test_identity_and_merge_events_still_update_accounts(self):
        self.bootstrap_admin()
        duplicate_id = self.make_user("operator.1042", person_id=1042)
        canonical_id = self.make_user("operator.2000", person_id=2000)
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
                "event_type": "people.identity.changed",
                "aggregate_id": "1042",
                "aggregate_version": 1,
                "payload": {"person_id": "1042", "display_name": "Иван Иванов", "name_version": 2},
            },
            session_factory=self.Session,
        )
        access_consumer.apply_access_event(
            {
                "event_id": str(uuid4()),
                "event_type": "people.merged",
                "aggregate_id": "1042",
                "aggregate_version": 2,
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


if __name__ == "__main__":
    unittest.main()
