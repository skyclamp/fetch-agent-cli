#!/usr/bin/env python3

import json
import re
import subprocess
import sys
from pathlib import Path


EXACT_MODEL_SLUGS = ["gpt-5.4", "gpt-5.5"]
MODEL_SLUG_PREFIXES = ["gpt-5.6-"]
MODEL_CATALOG_KEY_PATTERN = re.compile(
    r"^[ \t]*model_catalog_json[ \t]*=", re.MULTILINE
)


def bundled_model_catalog():
    result = subprocess.run(
        ["codex", "debug", "models", "--bundled"],
        check=True,
        stdout=subprocess.PIPE,
        universal_newlines=True,
        encoding="utf-8",
    )
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("Codex returned an invalid bundled model catalog: {}".format(error))

    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("Codex bundled model catalog does not contain a models array")
    return data


def extend_selected_context_windows(data):
    for model in data["models"]:
        slug = model.get("slug") if isinstance(model, dict) else None
        if slug in EXACT_MODEL_SLUGS or (
            isinstance(slug, str)
            and any(slug.startswith(prefix) for prefix in MODEL_SLUG_PREFIXES)
        ):
            model.update(
                context_window=1000000,
                max_context_window=1000000,
                effective_context_window_percent=92,
            )


def ensure_model_catalog_config(config_path, catalog_path):
    try:
        current = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""

    if MODEL_CATALOG_KEY_PATTERN.search(current) is not None:
        return False

    config_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_catalog_path = catalog_path.absolute()
    setting = "model_catalog_json = {}\n".format(
        json.dumps(str(absolute_catalog_path), ensure_ascii=False)
    )
    config_path.write_text(setting + current, encoding="utf-8")
    return True


def main():
    codex_dir = Path.home() / ".codex"
    config_path = codex_dir / "config.toml"
    catalog_path = codex_dir / "models-1m.json"

    data = bundled_model_catalog()
    extend_selected_context_windows(data)

    codex_dir.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    config_changed = ensure_model_catalog_config(config_path, catalog_path)

    print("Wrote extended Codex model catalog to {}.".format(catalog_path))
    if config_changed:
        print("Added model_catalog_json to {}.".format(config_path))
    else:
        print(
            "model_catalog_json is already configured in {}; "
            "left it unchanged.".format(config_path)
        )


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        UnicodeDecodeError,
    ) as error:
        print("Error: {}".format(error), file=sys.stderr)
        sys.exit(1)
