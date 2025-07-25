#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import json
import os
import re
from collections import namedtuple
from unittest import mock
from urllib.parse import quote

import pytest
import sqlalchemy
from cryptography.fernet import Fernet

from airflow.exceptions import AirflowException
from airflow.models import Connection, crypto
from airflow.sdk import BaseHook

from tests_common.test_utils.version_compat import SQLALCHEMY_V_1_4

sqlite = pytest.importorskip("airflow.providers.sqlite.hooks.sqlite")

from tests_common.test_utils.config import conf_vars
from tests_common.test_utils.markers import skip_if_force_lowest_dependencies_marker

ConnectionParts = namedtuple("ConnectionParts", ["conn_type", "login", "password", "host", "port", "schema"])

pytestmark = skip_if_force_lowest_dependencies_marker


@pytest.fixture
def get_connection1():
    return Connection()


@pytest.fixture
def get_connection2():
    return Connection(host="apache.org", extra={})


@pytest.fixture
def get_connection3():
    return Connection(conn_type="foo", login="", password="p@$$")


@pytest.fixture
def get_connection4():
    return Connection(
        conn_type="bar",
        description="Sample Description",
        host="example.org",
        login="user",
        password="p@$$",
        schema="schema",
        port=777,
        extra={"foo": "bar", "answer": 42},
    )


@pytest.fixture
def get_connection5():
    return Connection(uri="aws://")


class UriTestCaseConfig:
    def __init__(
        self,
        test_conn_uri: str,
        test_conn_attributes: dict,
        description: str,
    ):
        """

        :param test_conn_uri: URI that we use to create connection
        :param test_conn_attributes: we expect a connection object created with `test_uri` to have these
        attributes
        :param description: human-friendly name appended to parameterized test
        """
        self.test_uri = test_conn_uri
        self.test_conn_attributes = test_conn_attributes
        self.description = description

    @staticmethod
    def uri_test_name(func, num, param):
        return f"{func.__name__}_{num}_{param.args[0].description.replace(' ', '_')}"


class TestConnection:
    def setup_method(self):
        crypto._fernet = None
        self.patcher = mock.patch("airflow.models.connection.mask_secret", autospec=True)
        self.mask_secret = self.patcher.start()

    def teardown_method(self):
        crypto._fernet = None
        self.patcher.stop()

    @conf_vars({("core", "fernet_key"): ""})
    def test_connection_extra_no_encryption(self):
        """
        Tests extras on a new connection without encryption. The fernet key
        is set to a non-base64-encoded string and the extra is stored without
        encryption.
        """
        test_connection = Connection(extra='{"apache": "airflow"}')
        assert not test_connection.is_extra_encrypted
        assert test_connection.extra == '{"apache": "airflow"}'

    @conf_vars({("core", "fernet_key"): Fernet.generate_key().decode()})
    def test_connection_extra_with_encryption(self):
        """
        Tests extras on a new connection with encryption.
        """
        test_connection = Connection(extra='{"apache": "airflow"}')
        assert test_connection.is_extra_encrypted
        assert test_connection.extra == '{"apache": "airflow"}'

    def test_connection_extra_with_encryption_rotate_fernet_key(self):
        """
        Tests rotating encrypted extras.
        """
        key1 = Fernet.generate_key()
        key2 = Fernet.generate_key()

        with conf_vars({("core", "fernet_key"): key1.decode()}):
            test_connection = Connection(extra='{"apache": "airflow"}')
            assert test_connection.is_extra_encrypted
            assert test_connection.extra == '{"apache": "airflow"}'
            assert Fernet(key1).decrypt(test_connection._extra.encode()) == b'{"apache": "airflow"}'

        # Test decrypt of old value with new key
        with conf_vars({("core", "fernet_key"): f"{key2.decode()},{key1.decode()}"}):
            crypto._fernet = None
            assert test_connection.extra == '{"apache": "airflow"}'

            # Test decrypt of new value with new key
            test_connection.rotate_fernet_key()
            assert test_connection.is_extra_encrypted
            assert test_connection.extra == '{"apache": "airflow"}'
            assert Fernet(key2).decrypt(test_connection._extra.encode()) == b'{"apache": "airflow"}'

    test_from_uri_params = [
        UriTestCaseConfig(
            test_conn_uri="scheme://user:password@host%2Flocation:1234/schema",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host/location",
                schema="schema",
                login="user",
                password="password",
                port=1234,
                extra=None,
            ),
            description="without extras",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://user:password@host%2Flocation:1234/schema?"
            "extra1=a%20value&extra2=%2Fpath%2F",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host/location",
                schema="schema",
                login="user",
                password="password",
                port=1234,
                extra_dejson={"extra1": "a value", "extra2": "/path/"},
            ),
            description="with extras",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://user:password@host%2Flocation:1234/schema?"
            "__extra__=%7B%22my_val%22%3A+%5B%22list%22%2C+%22of%22%2C+%22values%22%5D%2C+%22extra%22%3A+%7B%22nested%22%3A+%7B%22json%22%3A+%22val%22%7D%7D%7D",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host/location",
                schema="schema",
                login="user",
                password="password",
                port=1234,
                extra_dejson={"my_val": ["list", "of", "values"], "extra": {"nested": {"json": "val"}}},
            ),
            description="with nested json",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://user:password@host%2Flocation:1234/schema?extra1=a%20value&extra2=",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host/location",
                schema="schema",
                login="user",
                password="password",
                port=1234,
                extra_dejson={"extra1": "a value", "extra2": ""},
            ),
            description="with empty extras",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://user:password@host%2Flocation%3Ax%3Ay:1234/schema?"
            "extra1=a%20value&extra2=%2Fpath%2F",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host/location:x:y",
                schema="schema",
                login="user",
                password="password",
                port=1234,
                extra_dejson={"extra1": "a value", "extra2": "/path/"},
            ),
            description="with colon in hostname",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://user:password%20with%20space@host%2Flocation%3Ax%3Ay:1234/schema",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host/location:x:y",
                schema="schema",
                login="user",
                password="password with space",
                port=1234,
            ),
            description="with encoded password",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://domain%2Fuser:password@host%2Flocation%3Ax%3Ay:1234/schema",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host/location:x:y",
                schema="schema",
                login="domain/user",
                password="password",
                port=1234,
            ),
            description="with encoded user",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://user:password%20with%20space@host:1234/schema%2Ftest",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host",
                schema="schema/test",
                login="user",
                password="password with space",
                port=1234,
            ),
            description="with encoded schema",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://user:password%20with%20space@host:1234",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host",
                schema="",
                login="user",
                password="password with space",
                port=1234,
            ),
            description="no schema",
        ),
        UriTestCaseConfig(
            test_conn_uri="google-cloud-platform://?key_path=%2Fkeys%2Fkey.json&scope="
            "https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform&project=airflow",
            test_conn_attributes=dict(
                conn_type="google_cloud_platform",
                host="",
                schema="",
                login=None,
                password=None,
                port=None,
                extra_dejson=dict(
                    key_path="/keys/key.json",
                    scope="https://www.googleapis.com/auth/cloud-platform",
                    project="airflow",
                ),
            ),
            description="with underscore",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://host:1234",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="host",
                schema="",
                login=None,
                password=None,
                port=1234,
            ),
            description="without auth info",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://%2FTmP%2F:1234",
            test_conn_attributes=dict(
                conn_type="scheme",
                host="/TmP/",
                schema="",
                login=None,
                password=None,
                port=1234,
            ),
            description="with path",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme:///airflow",
            test_conn_attributes=dict(
                conn_type="scheme",
                schema="airflow",
            ),
            description="schema only",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://@:1234",
            test_conn_attributes=dict(
                conn_type="scheme",
                port=1234,
            ),
            description="port only",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://:password%2F%21%40%23%24%25%5E%26%2A%28%29%7B%7D@",
            test_conn_attributes=dict(
                conn_type="scheme",
                password="password/!@#$%^&*(){}",
            ),
            description="password only",
        ),
        UriTestCaseConfig(
            test_conn_uri="scheme://login%2F%21%40%23%24%25%5E%26%2A%28%29%7B%7D@",
            test_conn_attributes=dict(
                conn_type="scheme",
                login="login/!@#$%^&*(){}",
            ),
            description="login only",
        ),
    ]

    @pytest.mark.parametrize("test_config", test_from_uri_params)
    def test_connection_from_uri(self, test_config: UriTestCaseConfig):
        connection = Connection(uri=test_config.test_uri)
        for conn_attr, expected_val in test_config.test_conn_attributes.items():
            actual_val = getattr(connection, conn_attr)
            if expected_val is None:
                assert expected_val is None
            if isinstance(expected_val, dict):
                assert expected_val == actual_val
            else:
                assert expected_val == actual_val

        expected_calls = []
        if test_config.test_conn_attributes.get("password"):
            expected_calls.append(mock.call(test_config.test_conn_attributes["password"]))
            expected_calls.append(mock.call(quote(test_config.test_conn_attributes["password"])))

        if test_config.test_conn_attributes.get("extra_dejson"):
            expected_calls.append(mock.call(test_config.test_conn_attributes["extra_dejson"]))

        self.mask_secret.assert_has_calls(expected_calls)

    @pytest.mark.parametrize("test_config", test_from_uri_params)
    def test_connection_get_uri_from_uri(self, test_config: UriTestCaseConfig):
        """
        This test verifies that when we create a conn_1 from URI, and we generate a URI from that conn, that
        when we create a conn_2 from the generated URI, we get an equivalent conn.
        1. Parse URI to create `Connection` object, `connection`.
        2. Using this connection, generate URI `generated_uri`..
        3. Using this`generated_uri`, parse and create new Connection `new_conn`.
        4. Verify that `new_conn` has same attributes as `connection`.
        """
        connection = Connection(uri=test_config.test_uri)
        generated_uri = connection.get_uri()
        new_conn = Connection(uri=generated_uri)
        assert connection.conn_type == new_conn.conn_type
        assert connection.login == new_conn.login
        assert connection.password == new_conn.password
        assert connection.host == new_conn.host
        assert connection.port == new_conn.port
        assert connection.schema == new_conn.schema
        assert connection.extra_dejson == new_conn.extra_dejson

    @pytest.mark.parametrize("test_config", test_from_uri_params)
    def test_connection_get_uri_from_conn(self, test_config: UriTestCaseConfig):
        """
        This test verifies that if we create conn_1 from attributes (rather than from URI), and we generate a
        URI, that when we create conn_2 from this URI, we get an equivalent conn.
        1. Build conn init params using `test_conn_attributes` and store in `conn_kwargs`
        2. Instantiate conn `connection` from `conn_kwargs`.
        3. Generate uri `get_uri` from this conn.
        4. Create conn `new_conn` from this uri.
        5. Verify `new_conn` has same attributes as `connection`.
        """
        conn_kwargs = {}
        for k, v in test_config.test_conn_attributes.items():
            if k == "extra_dejson":
                conn_kwargs.update({"extra": json.dumps(v)})
            else:
                conn_kwargs.update({k: v})

        connection = Connection(conn_id="test_conn", **conn_kwargs)  # type: ignore
        gen_uri = connection.get_uri()
        new_conn = Connection(conn_id="test_conn", uri=gen_uri)
        for conn_attr, expected_val in test_config.test_conn_attributes.items():
            actual_val = getattr(new_conn, conn_attr)
            if expected_val is None:
                assert actual_val is None
            else:
                assert actual_val == expected_val

    @pytest.mark.parametrize(
        "uri,uri_parts",
        [
            (
                "http://:password@host:80/database",
                ConnectionParts(
                    conn_type="http", login="", password="password", host="host", port=80, schema="database"
                ),
            ),
            (
                "http://user:@host:80/database",
                ConnectionParts(
                    conn_type="http", login="user", password=None, host="host", port=80, schema="database"
                ),
            ),
            (
                "http://user:password@/database",
                ConnectionParts(
                    conn_type="http", login="user", password="password", host="", port=None, schema="database"
                ),
            ),
            (
                "http://user:password@host:80/",
                ConnectionParts(
                    conn_type="http", login="user", password="password", host="host", port=80, schema=""
                ),
            ),
            (
                "http://user:password@/",
                ConnectionParts(
                    conn_type="http", login="user", password="password", host="", port=None, schema=""
                ),
            ),
            (
                "postgresql://user:password@%2Ftmp%2Fz6rqdzqh%2Fexample%3Awest1%3Atestdb/testdb",
                ConnectionParts(
                    conn_type="postgres",
                    login="user",
                    password="password",
                    host="/tmp/z6rqdzqh/example:west1:testdb",
                    port=None,
                    schema="testdb",
                ),
            ),
            (
                "postgresql://user@%2Ftmp%2Fz6rqdzqh%2Fexample%3Aeurope-west1%3Atestdb/testdb",
                ConnectionParts(
                    conn_type="postgres",
                    login="user",
                    password=None,
                    host="/tmp/z6rqdzqh/example:europe-west1:testdb",
                    port=None,
                    schema="testdb",
                ),
            ),
            (
                "postgresql://%2Ftmp%2Fz6rqdzqh%2Fexample%3Aeurope-west1%3Atestdb",
                ConnectionParts(
                    conn_type="postgres",
                    login=None,
                    password=None,
                    host="/tmp/z6rqdzqh/example:europe-west1:testdb",
                    port=None,
                    schema="",
                ),
            ),
            (
                "spark://k8s%3a%2F%2F100.68.0.1:443?deploy-mode=cluster",
                ConnectionParts(
                    conn_type="spark",
                    login=None,
                    password=None,
                    host="k8s://100.68.0.1",
                    port=443,
                    schema="",
                ),
            ),
            (
                "spark://user:password@k8s%3a%2F%2F100.68.0.1:443?deploy-mode=cluster",
                ConnectionParts(
                    conn_type="spark",
                    login="user",
                    password="password",
                    host="k8s://100.68.0.1",
                    port=443,
                    schema="",
                ),
            ),
            (
                "spark://user@k8s%3a%2F%2F100.68.0.1:443?deploy-mode=cluster",
                ConnectionParts(
                    conn_type="spark",
                    login="user",
                    password=None,
                    host="k8s://100.68.0.1",
                    port=443,
                    schema="",
                ),
            ),
            (
                "spark://k8s%3a%2F%2Fno.port.com?deploy-mode=cluster",
                ConnectionParts(
                    conn_type="spark",
                    login=None,
                    password=None,
                    host="k8s://no.port.com",
                    port=None,
                    schema="",
                ),
            ),
        ],
    )
    def test_connection_from_with_auth_info(self, uri, uri_parts):
        connection = Connection(uri=uri)

        assert connection.conn_type == uri_parts.conn_type
        assert connection.login == uri_parts.login
        assert connection.password == uri_parts.password
        assert connection.host == uri_parts.host
        assert connection.port == uri_parts.port
        assert connection.schema == uri_parts.schema

    @pytest.mark.parametrize(
        "extra, expected",
        [
            ('{"extra": null}', None),
            ('{"extra": {"yo": "hi"}}', '{"yo": "hi"}'),
            ('{"extra": "{\\"yo\\": \\"hi\\"}"}', '{"yo": "hi"}'),
        ],
    )
    def test_from_json_extra(self, extra, expected):
        """Json serialization should support extra stored as object _or_ as object string representation"""
        assert Connection.from_json(extra).extra == expected

    @pytest.mark.parametrize(
        "val,expected",
        [
            ('{"conn_type": "abc-abc"}', "abc_abc"),
            ('{"conn_type": "abc_abc"}', "abc_abc"),
            ('{"conn_type": "postgresql"}', "postgres"),
        ],
    )
    def test_from_json_conn_type(self, val, expected):
        """Two conn_type normalizations are applied: replace - with _ and postgresql with postgres"""
        assert Connection.from_json(val).conn_type == expected

    @pytest.mark.parametrize(
        "val,expected",
        [
            ('{"port": 1}', 1),
            ('{"port": "1"}', 1),
            ('{"port": null}', None),
        ],
    )
    def test_from_json_port(self, val, expected):
        """Two conn_type normalizations are applied: replace - with _ and postgresql with postgres"""
        assert Connection.from_json(val).port == expected

    @pytest.mark.parametrize(
        "val,expected",
        [
            ('pass :/!@#$%^&*(){}"', 'pass :/!@#$%^&*(){}"'),  # these are the same
            (None, None),
            ("", None),  # this is a consequence of the password getter
        ],
    )
    def test_from_json_special_characters(self, val, expected):
        """Two conn_type normalizations are applied: replace - with _ and postgresql with postgres"""
        json_val = json.dumps(dict(password=val))
        assert Connection.from_json(json_val).password == expected

    @mock.patch.dict(
        "os.environ",
        {
            "AIRFLOW_CONN_TEST_URI": "postgresql://username:password%21@ec2.compute.com:5432/the_database",
        },
    )
    def test_using_env_var(self):
        from airflow.providers.sqlite.hooks.sqlite import SqliteHook

        conn = SqliteHook.get_connection(conn_id="test_uri")
        assert conn.host == "ec2.compute.com"
        assert conn.schema == "the_database"
        assert conn.login == "username"
        assert conn.password == "password!"
        assert conn.port == 5432

        self.mask_secret.assert_has_calls([mock.call("password!"), mock.call(quote("password!"))])

    @mock.patch.dict(
        "os.environ",
        {
            "AIRFLOW_CONN_TEST_URI_NO_CREDS": "postgresql://ec2.compute.com/the_database",
        },
    )
    def test_using_unix_socket_env_var(self):
        from airflow.providers.sqlite.hooks.sqlite import SqliteHook

        conn = SqliteHook.get_connection(conn_id="test_uri_no_creds")
        assert conn.host == "ec2.compute.com"
        assert conn.schema == "the_database"
        assert conn.login is None
        assert conn.password is None
        assert conn.port is None

    def test_param_setup(self):
        conn = Connection(
            conn_id="local_mysql",
            conn_type="mysql",
            host="localhost",
            login="airflow",
            password="airflow",
            schema="airflow",
        )
        assert conn.host == "localhost"
        assert conn.schema == "airflow"
        assert conn.login == "airflow"
        assert conn.password == "airflow"
        assert conn.port is None

    @pytest.mark.db_test
    def test_env_var_priority(self, mock_supervisor_comms):
        from airflow.providers.sqlite.hooks.sqlite import SqliteHook
        from airflow.sdk.execution_time.comms import ConnectionResult

        conn = ConnectionResult(
            conn_id="airflow_db",
            conn_type="mysql",
            host="mysql",
            login="root",
        )

        mock_supervisor_comms.send.return_value = conn

        conn = SqliteHook.get_connection(conn_id="airflow_db")
        assert conn.host != "ec2.compute.com"

        with mock.patch.dict(
            "os.environ",
            {
                "AIRFLOW_CONN_AIRFLOW_DB": "postgresql://username:password@ec2.compute.com:5432/the_database",
            },
        ):
            conn = SqliteHook.get_connection(conn_id="airflow_db")
            assert conn.host == "ec2.compute.com"
            assert conn.schema == "the_database"
            assert conn.login == "username"
            assert conn.password == "password"
            assert conn.port == 5432

    @mock.patch.dict(
        "os.environ",
        {
            "AIRFLOW_CONN_TEST_URI": "postgresql://username:password@ec2.compute.com:5432/the_database",
            "AIRFLOW_CONN_TEST_URI_NO_CREDS": "postgresql://ec2.compute.com/the_database",
        },
    )
    def test_dbapi_get_uri(self):
        conn = BaseHook.get_connection(conn_id="test_uri")
        hook = conn.get_hook()
        assert hook.get_uri() == "postgresql://username:password@ec2.compute.com:5432/the_database"
        conn2 = BaseHook.get_connection(conn_id="test_uri_no_creds")
        hook2 = conn2.get_hook()
        assert hook2.get_uri() == "postgresql://ec2.compute.com/the_database"

    @mock.patch.dict(
        "os.environ",
        {
            "AIRFLOW_CONN_TEST_URI": "postgresql://username:password@ec2.compute.com:5432/the_database",
            "AIRFLOW_CONN_TEST_URI_NO_CREDS": "postgresql://ec2.compute.com/the_database",
        },
    )
    def test_dbapi_get_sqlalchemy_engine(self):
        conn = BaseHook.get_connection(conn_id="test_uri")
        hook = conn.get_hook()
        engine = hook.get_sqlalchemy_engine()
        expected = "postgresql://username:password@ec2.compute.com:5432/the_database"
        assert isinstance(engine, sqlalchemy.engine.Engine)
        if SQLALCHEMY_V_1_4:
            assert str(engine.url) == expected
        else:
            assert engine.url.render_as_string(hide_password=False) == expected

    @mock.patch.dict(
        "os.environ",
        {
            "AIRFLOW_CONN_TEST_URI": "postgresql://username:password@ec2.compute.com:5432/the_database",
            "AIRFLOW_CONN_TEST_URI_NO_CREDS": "postgresql://ec2.compute.com/the_database",
        },
    )
    def test_get_connections_env_var(self):
        from airflow.providers.sqlite.hooks.sqlite import SqliteHook

        conns = SqliteHook.get_connection(conn_id="test_uri")
        assert conns.host == "ec2.compute.com"
        assert conns.schema == "the_database"
        assert conns.login == "username"
        assert conns.password == "password"
        assert conns.port == 5432

    def test_connection_mixed(self):
        with pytest.raises(
            AirflowException,
            match=re.escape(
                "You must create an object using the URI or individual values (conn_type, host, login, "
                "password, schema, port or extra).You can't mix these two ways to create this object."
            ),
        ):
            Connection(conn_id="TEST_ID", uri="mysql://", schema="AAA")

    @pytest.mark.db_test
    def test_masking_from_db(self):
        """Test secrets are masked when loaded directly from the DB"""
        from airflow.settings import Session

        session = Session()

        try:
            conn = Connection(
                conn_id=f"test-{os.getpid()}",
                conn_type="http",
                password="s3cr3t!",
                extra='{"apikey":"masked too"}',
            )
            session.add(conn)
            session.flush()

            # Make sure we re-load it, not just get the cached object back
            session.expunge(conn)

            self.mask_secret.reset_mock()

            from_db = session.get(Connection, conn.id)
            from_db.extra_dejson

            assert self.mask_secret.mock_calls == [
                # We should have called it _again_ when loading from the DB
                mock.call("s3cr3t!"),
                mock.call(quote("s3cr3t!")),
                mock.call({"apikey": "masked too"}),
            ]
        finally:
            session.rollback()

    @mock.patch.dict(
        "os.environ",
        {
            "AIRFLOW_CONN_TEST_URI": "sqlite://",
        },
    )
    def test_connection_test_success(self):
        conn = Connection(conn_id="test_uri", conn_type="sqlite")
        res = conn.test_connection()
        assert res[0] is True
        assert res[1] == "Connection successfully tested"

    @mock.patch.dict(
        "os.environ",
        {
            "AIRFLOW_CONN_TEST_URI_NO_HOOK": "unknown://",
        },
    )
    def test_connection_test_no_hook(self):
        conn = Connection(conn_id="test_uri_no_hook", conn_type="unknown")
        res = conn.test_connection()
        assert res[0] is False
        assert res[1] == 'Unknown hook type "unknown"'

    @mock.patch.dict(
        "os.environ",
        {
            "AIRFLOW_CONN_TEST_URI_HOOK_METHOD_MISSING": "grpc://",
        },
    )
    def test_connection_test_hook_method_missing(self):
        conn = Connection(conn_id="test_uri_hook_method_missing", conn_type="grpc")
        res = conn.test_connection()
        assert res[0] is False
        assert res[1] == "Hook GrpcHook doesn't implement or inherit test_connection method"

    def test_extra_warnings_non_json(self):
        with pytest.raises(ValueError, match="non-JSON"):
            Connection(conn_id="test_extra", conn_type="none", extra="hi")

    def test_extra_warnings_non_dict_json(self):
        with pytest.raises(ValueError, match="not parse as a dictionary"):
            Connection(conn_id="test_extra", conn_type="none", extra='"hi"')

    def test_get_uri_no_conn_type(self):
        # no conn type --> scheme-relative URI
        assert Connection().get_uri() == "//"
        # with host, still works
        assert Connection(host="abc").get_uri() == "//abc"
        # parsing back as conn still works
        assert Connection(uri="//abc").host == "abc"

    @pytest.mark.parametrize(
        "conn, expected_json",
        [
            pytest.param("get_connection1", "{}", id="empty"),
            pytest.param("get_connection2", '{"host": "apache.org"}', id="empty-extra"),
            pytest.param(
                "get_connection3",
                '{"conn_type": "foo", "login": "", "password": "p@$$"}',
                id="some-fields",
            ),
            pytest.param(
                "get_connection4",
                json.dumps(
                    {
                        "conn_type": "bar",
                        "description": "Sample Description",
                        "host": "example.org",
                        "login": "user",
                        "password": "p@$$",
                        "schema": "schema",
                        "port": 777,
                        "extra": {"foo": "bar", "answer": 42},
                    }
                ),
                id="all-fields",
            ),
            pytest.param(
                "get_connection5",
                # During parsing URI some of the fields evaluated as an empty strings
                '{"conn_type": "aws", "host": "", "schema": ""}',
                id="uri",
            ),
        ],
    )
    def test_as_json_from_connection(self, conn: Connection, expected_json, request):
        conn = request.getfixturevalue(conn)
        result = conn.as_json()
        assert result == expected_json
        restored_conn = Connection.from_json(result)

        assert restored_conn.conn_type == conn.conn_type
        assert restored_conn.description == conn.description
        assert restored_conn.host == conn.host
        assert restored_conn.password == conn.password
        assert restored_conn.schema == conn.schema
        assert restored_conn.port == conn.port
        assert restored_conn.extra_dejson == conn.extra_dejson
