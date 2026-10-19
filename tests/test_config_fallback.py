"""config.toml.example fallback + example-file validity.

config.toml is gitignored (it holds real downstream URLs and API keys), so a
fresh clone and every CI checkout has only config.toml.example. Importing
app.core.config must work there — several test modules import it directly,
outside the conftest patches.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.core import config as cfg


class TestActiveConfigPath:
    def test_prefers_real_config_when_present(self, tmp_path):
        real = tmp_path / "config.toml"
        real.write_text("[app]\n", encoding="utf-8")
        with patch.object(cfg, "_CONFIG_PATH", real):
            assert cfg._active_config_path() == real

    def test_falls_back_to_example_when_missing(self, tmp_path):
        with patch.object(cfg, "_CONFIG_PATH", tmp_path / "nope.toml"):
            assert cfg._active_config_path() == cfg._EXAMPLE_CONFIG_PATH

    def test_mtime_is_zero_when_nothing_exists(self, tmp_path):
        with (
            patch.object(cfg, "_CONFIG_PATH", tmp_path / "nope.toml"),
            patch.object(cfg, "_EXAMPLE_CONFIG_PATH", tmp_path / "also-nope.toml"),
        ):
            assert cfg._config_mtime_now() == 0.0


class TestExampleConfigIsUsable:
    def test_example_file_ships_with_the_repo(self):
        assert cfg._EXAMPLE_CONFIG_PATH.exists(), (
            "config.toml.example is the fallback every fresh checkout boots "
            "from — it must stay in the repo"
        )

    def test_example_parses_and_builds(self, tmp_path):
        """Guards the example from rotting out of sync with the loader."""
        with patch.object(cfg, "_CONFIG_PATH", tmp_path / "nope.toml"):
            raw = cfg._load_toml()
        (
            app_config, model_routing, pricing_map, fallback_map,
            azure_models, azure_fallback, bedrock_models, bedrock_fallback,
        ) = cfg._build_config(raw)

        assert isinstance(app_config, dict)
        assert model_routing, "the example should configure at least one vLLM model"
        for alias, route in model_routing.items():
            assert route.get("base_url"), f"{alias} is missing base_url"
            assert route.get("real_model"), f"{alias} is missing real_model"
            assert route.get("type"), f"{alias} is missing its injected type tag"
        assert "_default" in pricing_map
        for target in list(fallback_map.values()):
            assert target in model_routing, f"[fallback] points at unknown alias {target}"
        for target in list(azure_fallback.values()):
            assert target in azure_models
        for target in list(bedrock_fallback.values()):
            assert target in bedrock_models

    def test_writes_still_target_the_real_config(self):
        """The fallback is read-only — save_config must never write the example."""
        source = Path(cfg.__file__).read_text(encoding="utf-8")
        assert "os.replace(tmp_path, _EXAMPLE_CONFIG_PATH)" not in source
        assert source.count("os.replace(tmp_path, _CONFIG_PATH)") == 2


class TestBedrockIamCredentials:
    def test_iam_keys_survive_build_and_save(self, tmp_path):
        """aws_access_key_id / aws_secret_access_key load into BEDROCK_MODELS
        and an admin save writes them back (blank ones are left out)."""
        import toml

        raw = {"bedrock_models": {"br": {
            "type": "llm", "region": "ap-northeast-1", "model_id": "anthropic.x",
            "aws_access_key_id": "AKIA", "aws_secret_access_key": "s",
        }}}
        bedrock = cfg._build_config(raw)[6]
        assert bedrock["br"]["aws_access_key_id"] == "AKIA"
        assert bedrock["br"]["aws_secret_access_key"] == "s"

        path = tmp_path / "config.toml"
        path.write_text("")
        with patch.object(cfg, "_CONFIG_PATH", path), patch.object(cfg, "reload_config"):
            cfg.save_config({}, {}, {}, bedrock_models={
                **bedrock, "bearer": {"type": "llm", "model_id": "m", "api_key": "k",
                                      "aws_access_key_id": "", "aws_secret_access_key": ""},
            })
        saved = toml.load(path)["bedrock_models"]
        assert saved["br"]["aws_access_key_id"] == "AKIA"
        assert saved["br"]["aws_secret_access_key"] == "s"
        assert "aws_access_key_id" not in saved["bearer"]
        # No region set → none written, so BEDROCK_REGION keeps applying.
        assert "region" not in saved["bearer"]
        assert "region" not in cfg._build_config({"bedrock_models": {"b": {"model_id": "m"}}})[6]["b"]
