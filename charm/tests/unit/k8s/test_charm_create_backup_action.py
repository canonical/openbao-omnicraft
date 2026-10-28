#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


import ops.testing as testing
import pytest
from openbao.openbao_managers import ManagerError

from fixtures import OpenBaoCharmFixtures


class TestCharmCreateBackupAction(OpenBaoCharmFixtures):
    def test_given_failed_to_initialize_openbao_client_when_create_backup_then_action_fails(self):
        self.mock_s3_requirer.configure_mock(
            **{
                "get_s3_connection_info.return_value": {
                    "access-key": "my-access-key",
                    "secret-key": "my-secret-key",
                    "endpoint": "my-endpoint",
                    "bucket": "my bucket",
                    "region": "my-region",
                },
            },
        )
        container = testing.Container(
            name="openbao",
            can_connect=True,
        )
        s3_relation = testing.Relation(
            endpoint="s3-parameters",
            interface="s3",
        )
        state_in = testing.State(
            containers=[container],
            leader=True,
            relations=[s3_relation],
        )
        with pytest.raises(testing.ActionFailed) as e:
            self.ctx.run(self.ctx.on.action("create-backup"), state_in)
        assert e.value.message == "Failed to initialize OpenBao client."

    def test_given_manager_raises_error_when_create_backup_then_action_fails(self):
        self.mock_s3_requirer.configure_mock(
            **{
                "get_s3_connection_info.return_value": {
                    "access-key": "my-access-key",
                    "secret-key": "my-secret-key",
                    "endpoint": "my-endpoint",
                    "bucket": "my bucket",
                    "region": "my-region",
                },
            },
        )
        self.mock_backup_manager.create_backup.side_effect = ManagerError("some error message")
        self.mock_openbao.configure_mock(
            **{
                "is_api_available.return_value": True,
                "is_active_or_standby.return_value": True,
            },
        )
        approle_secret = testing.Secret(
            label="openbao-approle-auth-details",
            tracked_content={"role-id": "role id", "secret-id": "secret id"},
        )
        container = testing.Container(
            name="openbao",
            can_connect=True,
        )
        s3_relation = testing.Relation(
            endpoint="s3-parameters",
            interface="s3",
        )
        state_in = testing.State(
            containers=[container],
            leader=True,
            relations=[s3_relation],
            secrets=[approle_secret],
        )
        with pytest.raises(testing.ActionFailed) as e:
            self.ctx.run(self.ctx.on.action("create-backup"), state_in)
        assert e.value.message == "Failed to create backup: some error message"
