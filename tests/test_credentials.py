"""
Tests for the credential manager.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

import sherlock.core.credentials as credentials_module
from sherlock.core.credentials import (
    CredentialManager,
    Credentials,
    describe_keyring_backend,
    ensure_secure_keyring,
)
from sherlock.core.exceptions import CredentialError


def _fake_backend(module: str, name: str = "Keyring", chained: list | None = None):
    """Build a fake keyring backend instance with a chosen module path."""
    cls = type(name, (), {})
    cls.__module__ = module
    instance = cls()
    if chained is not None:
        instance.backends = chained
    return instance


class TestCredentials:
    """Tests for the Credentials pydantic model."""

    def test_endpoint_us(self):
        """US region returns the US endpoint."""
        creds = Credentials(account_id="123", api_key="NRAK-abc", region="US")
        assert creds.endpoint == "https://api.newrelic.com/graphql"

    def test_endpoint_eu(self):
        """EU region returns the EU endpoint."""
        creds = Credentials(account_id="123", api_key="NRAK-abc", region="EU")
        assert creds.endpoint == "https://api.eu.newrelic.com/graphql"

    def test_redacted_key(self):
        """API key is properly redacted."""
        creds = Credentials(account_id="123", api_key="NRAK-abcdefgh12345678", region="US")
        redacted = creds.redacted_key
        assert "NRAK" in redacted
        assert "5678" in redacted
        assert "abcdefgh" not in redacted

    def test_api_key_excluded_from_serialization(self):
        """API key is not included in model_dump output."""
        creds = Credentials(account_id="123", api_key="NRAK-secret", region="US")
        dumped = creds.model_dump()
        assert "NRAK-secret" not in json.dumps(dumped)

    def test_short_key_redaction(self):
        """Short API keys are fully redacted."""
        creds = Credentials(account_id="123", api_key="short", region="US")
        assert creds.redacted_key == "****"


class TestCredentialManager:
    """Tests for the CredentialManager class."""

    @patch("sherlock.core.credentials.PROFILES_FILE")
    @patch("sherlock.core.credentials.CONFIG_DIR")
    @patch("sherlock.core.credentials.keyring")
    def test_save_and_load_profile(
        self, mock_keyring, mock_config_dir, mock_profiles_file, tmp_path
    ):
        """Profiles can be saved and loaded."""
        profiles_file = tmp_path / "profiles.json"
        profiles_file.write_text("[]")
        mock_profiles_file.__truediv__ = lambda s, o: (
            profiles_file if "profiles" in str(o) else tmp_path / o
        )
        mock_config_dir.mkdir = MagicMock()

        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles_file),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
        ):
            manager = CredentialManager()
            manager.save_profile("test-prof", "123456", "NRAK-key123", "US")

            mock_keyring.set_password.assert_called_once_with(
                "sherlock", "test-prof", "NRAK-key123"
            )

            data = json.loads(profiles_file.read_text())
            assert len(data) == 1
            assert data[0]["name"] == "test-prof"
            assert data[0]["account_id"] == "123456"

    @patch("sherlock.core.credentials.keyring")
    def test_list_profiles(self, mock_keyring, tmp_path):
        """List profiles returns all saved profiles."""
        profiles_file = tmp_path / "profiles.json"
        profiles_file.write_text(
            json.dumps(
                [
                    {
                        "name": "prod",
                        "account_id": "111",
                        "region": "US",
                        "created_at": "2025-01-01",
                    },
                    {
                        "name": "stg",
                        "account_id": "222",
                        "region": "EU",
                        "created_at": "2025-01-02",
                    },
                ]
            )
        )

        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles_file),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
        ):
            manager = CredentialManager()
            profiles = manager.list_profiles()
            assert len(profiles) == 2
            assert profiles[0]["name"] == "prod"

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_credentials_success(self):
        """Successful credential validation."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "actor": {
                            "user": {"name": "Test User", "email": "test@example.com"},
                            "account": {"name": "Test Account"},
                        }
                    }
                },
            )
        )

        with (
            patch("sherlock.core.credentials.CONFIG_DIR", Path("/tmp/test-mcp")),
            patch("sherlock.core.credentials.PROFILES_FILE", Path("/tmp/test-mcp/profiles.json")),
        ):
            manager = CredentialManager()
            result = await manager.validate_credentials("123", "NRAK-test", "US")

        assert result["valid"] is True
        assert result["user_name"] == "Test User"
        assert result["account_name"] == "Test Account"

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_credentials_unauthorized(self):
        """Invalid API key returns valid=False."""
        respx.post("https://api.newrelic.com/graphql").mock(return_value=httpx.Response(401))

        with (
            patch("sherlock.core.credentials.CONFIG_DIR", Path("/tmp/test-mcp")),
            patch("sherlock.core.credentials.PROFILES_FILE", Path("/tmp/test-mcp/profiles.json")),
        ):
            manager = CredentialManager()
            result = await manager.validate_credentials("123", "bad-key", "US")

        assert result["valid"] is False
        assert "401" in result["error"]


class TestKeyringHardening:
    """S5 — weak keyring backends must fail loudly."""

    def setup_method(self):
        credentials_module._insecure_keyring_warned = False

    def _patch_backend(self, backend):
        mock_keyring = MagicMock()
        mock_keyring.get_keyring.return_value = backend
        return patch("sherlock.core.credentials.keyring", mock_keyring)

    def test_secure_backend_detected(self):
        backend = _fake_backend("keyring.backends.Windows", "WinVaultKeyring")
        with self._patch_backend(backend):
            info = describe_keyring_backend()
        assert info["secure"] is True
        assert info["weak"] == []

    def test_fail_backend_detected_as_weak(self):
        backend = _fake_backend("keyring.backends.fail")
        with self._patch_backend(backend):
            info = describe_keyring_backend()
        assert info["secure"] is False
        assert info["weak"] == ["keyring.backends.fail.Keyring"]

    def test_plaintext_alt_backend_detected_as_weak(self):
        backend = _fake_backend("keyrings.alt.file", "PlaintextKeyring")
        with self._patch_backend(backend):
            assert describe_keyring_backend()["secure"] is False

    def test_chainer_with_weak_member_is_weak(self):
        chainer = _fake_backend(
            "keyring.backends.chainer",
            "ChainerBackend",
            chained=[
                _fake_backend("keyring.backends.macOS"),
                _fake_backend("keyrings.alt.file", "PlaintextKeyring"),
            ],
        )
        with self._patch_backend(chainer):
            info = describe_keyring_backend()
        assert info["secure"] is False
        assert "keyrings.alt.file.PlaintextKeyring" in info["weak"]

    def test_ensure_raises_with_guidance(self, monkeypatch):
        monkeypatch.delenv("SHERLOCK_ALLOW_INSECURE_KEYRING", raising=False)
        with self._patch_backend(_fake_backend("keyring.backends.null")):
            with pytest.raises(CredentialError) as excinfo:
                ensure_secure_keyring("store")
        message = str(excinfo.value)
        assert "keyring.backends.null.Keyring" in message
        assert "SHERLOCK_ALLOW_INSECURE_KEYRING" in message

    def test_env_override_allows_weak_backend(self, monkeypatch):
        monkeypatch.setenv("SHERLOCK_ALLOW_INSECURE_KEYRING", "1")
        with self._patch_backend(_fake_backend("keyring.backends.null")):
            ensure_secure_keyring("store")  # must not raise

    def test_save_profile_refuses_weak_backend(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SHERLOCK_ALLOW_INSECURE_KEYRING", raising=False)
        profiles_file = tmp_path / "profiles.json"
        profiles_file.write_text("[]")
        mock_keyring = MagicMock()
        mock_keyring.get_keyring.return_value = _fake_backend("keyrings.alt.file")

        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles_file),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
            patch("sherlock.core.credentials.keyring", mock_keyring),
        ):
            manager = CredentialManager()
            with pytest.raises(CredentialError):
                manager.save_profile("prof", "123456", "NRAK-key", "US")
            # The key must never reach the weak backend.
            mock_keyring.set_password.assert_not_called()

    def test_load_profile_refuses_weak_backend(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SHERLOCK_ALLOW_INSECURE_KEYRING", raising=False)
        profiles_file = tmp_path / "profiles.json"
        profiles_file.write_text(
            json.dumps([{"name": "prof", "account_id": "123456", "region": "US"}])
        )
        mock_keyring = MagicMock()
        mock_keyring.get_keyring.return_value = _fake_backend("keyring.backends.fail")

        with (
            patch("sherlock.core.credentials.PROFILES_FILE", profiles_file),
            patch("sherlock.core.credentials.CONFIG_DIR", tmp_path),
            patch("sherlock.core.credentials.keyring", mock_keyring),
        ):
            manager = CredentialManager()
            with pytest.raises(CredentialError):
                manager.load_profile("prof")
