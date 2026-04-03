from .config import RepoPaths

OVERLAY_SCHEMA_VERSION = 1


def ensure_overlay_layout(paths: RepoPaths) -> None:
    paths.local_overlay.mkdir(parents=True, exist_ok=True)
    defaults = {
        paths.local_servers_file: "version: 1\nservers: {}\n",
        paths.local_auth_file: "version: 1\nssh_auth: {refs: {}}\ngit_auth: {refs: {}}\n",
        paths.local_repos_file: "version: 1\nworkspace: {}\nsubmodules: {}\n",
    }
    for file_path, content in defaults.items():
        if not file_path.exists():
            file_path.write_text(content, encoding="utf-8")
