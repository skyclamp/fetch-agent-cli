#!/usr/bin/env python3

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


EXACT_MODEL_SLUGS = {"gpt-5.5"}
MODEL_SLUG_PREFIXES = ("gpt-5.6-",)
CONTEXT_PROFILES = {
    "272k": {
        "context_window": 272000,
        "max_context_window": 1000000,
    },
    "372k": {
        "context_window": 372000,
        "max_context_window": 1000000,
    },
    "1m": {
        "context_window": 1000000,
        "max_context_window": 1000000,
    },
}
CONTEXT_CHOICES = ("272k", "372k", "1m")
MODEL_CATALOG_KEY_PATTERN = re.compile(
    r"^[ \t]*model_catalog_json[ \t]*=[^\r\n]*(?:\r?\n|$)", re.MULTILINE
)
TOML_TABLE_PATTERN = re.compile(r"^[ \t]*\[", re.MULTILINE)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Configure the context window for bundled Codex 5.5 and 5.6 models."
    )
    parser.add_argument(
        "--context-window",
        choices=CONTEXT_CHOICES,
        default="272k",
        help="use a 272k, 372k, or 1M context with a 1M override ceiling "
        "(default: 1m)",
    )
    return parser.parse_args()


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


def configure_context_windows(data, context_profile):
    settings = CONTEXT_PROFILES[context_profile]
    configured_slugs = []
    for model in data["models"]:
        slug = model.get("slug") if isinstance(model, dict) else None
        if slug in EXACT_MODEL_SLUGS or (
            isinstance(slug, str)
            and any(slug.startswith(prefix) for prefix in MODEL_SLUG_PREFIXES)
        ):
            model.pop("auto_compact_token_limit", None)
            model.update(settings)
            configured_slugs.append(slug)

    if not configured_slugs:
        raise RuntimeError(
            "Codex bundled model catalog does not contain any 5.5 or 5.6 models"
        )
    return configured_slugs


def ensure_model_catalog_config(config_path, catalog_path):
    try:
        current = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""

    absolute_catalog_path = catalog_path.absolute()
    catalog_path_text = str(absolute_catalog_path)
    setting = "model_catalog_json = {}\n".format(
        json.dumps(catalog_path_text, ensure_ascii=False)
    )

    first_table = TOML_TABLE_PATTERN.search(current)
    root_end = first_table.start() if first_table is not None else len(current)
    match = MODEL_CATALOG_KEY_PATTERN.search(current, 0, root_end)
    if match is None:
        updated = setting + current
        status = "added"
    elif match.group(0) == setting:
        return "unchanged"
    else:
        updated = current[: match.start()] + setting + current[match.end() :]
        status = "updated"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(updated, encoding="utf-8")
    return status


def main():
    args = parse_args()
    codex_dir = Path.home() / ".codex"
    config_path = codex_dir / "config.toml"
    catalog_path = codex_dir / "my-model-catalog.json"

    data = bundled_model_catalog()
    configured_slugs = configure_context_windows(data, args.context_window)

    codex_dir.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    config_status = ensure_model_catalog_config(config_path, catalog_path)

    print(
        "Configured {} context for {} in {}.".format(
            args.context_window, ", ".join(configured_slugs), catalog_path
        )
    )
    print("{} model_catalog_json in {}.".format(config_status.capitalize(), config_path))


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
