# Audacy

Audacy package for Jarvis

## Development

```bash
jdt test .              # Run Pantry-compatible tests
jdt test . -v           # Verbose output
jdt test . --install-deps  # Auto-install pip deps before testing
jdt validate .          # Fast manifest-only check
jdt manifest .          # Regenerate manifest from code
jdt deploy local .      # Install to local node
```

## Package Structure

This is a Jarvis package with the following components:

- **audacy** — Voice command (IJarvisCommand) (`commands/audacy/command.py`)

## Key Rules

- **Logging**: Use the `try: from jarvis_log_client` pattern (see existing stubs)
- **Errors**: Never raise from `run()` — return `CommandResponse.error_response()` or `DeviceControlResult(success=False, error="...")`
- **Data access**: Use `JarvisStorage` for secrets and persistent data, never raw SQLite/SQLAlchemy
- **Shared code**: If you add shared modules, name the directory `audacy_shared/` (not `shared/`, `lib/`, `helpers/` — those collide on sys.path after install)
- **`context_data["message"]`**: This key is what gets spoken aloud by TTS

## Manifest

The `jarvis_package.yaml` declares:
- **secrets** — credentials/config the user must provide (shown in mobile app settings)
- **packages** — pip dependencies installed on the node
- **components** — entry points for each component (used by the installer)

Run `jdt manifest . --non-interactive` to regenerate the manifest from your code after making changes.

## Testing

`jdt test` checks three things:
1. Manifest is valid (schema, semver, categories, paths exist)
2. Static analysis passes (correct base class, required methods, no dangerous imports)
3. Import succeeds and properties return correct types

All stubs pass out of the box — if tests break after your changes, check the error messages for what to fix.

## SDK Quick Reference

### CommandResponse
```python
CommandResponse.success_response(context_data={"message": "spoken text"}, wait_for_input=False)
CommandResponse.error_response(error_details="what went wrong")
CommandResponse.follow_up_response(context_data={"message": "what else?"})
```

### JarvisStorage
```python
from jarvis_command_sdk import JarvisStorage
storage = JarvisStorage("audacy")
api_key = storage.get_secret("MY_API_KEY", scope="integration")
storage.save(key="cache", data={"result": "value"})
data = storage.get(key="cache")
```

### JarvisParameter
```python
JarvisParameter(name="city", param_type="string", required=True, description="City name")
JarvisParameter(name="count", param_type="int", required=False, default="5", description="How many")
JarvisParameter(name="unit", param_type="string", enum_values=["imperial", "metric"], description="Units")
```

### JarvisSecret
```python
JarvisSecret(key="API_KEY", description="Service API key", scope="integration",
             value_type="string", is_sensitive=True, required=True, friendly_name="API Key")
```

## Loop (agents)

**Audacy**: Stream live radio from the Audacy network (sports, news, talk, music) on this node — direct stream playback through mpd, no Music Assistant or other external server required. A Jarvis package (command) that nodes install through Pantry.

**Verify** (what CI runs: `.github/workflows/ci.yml` calls the toolkit's reusable `package-ci.yml`) (no `tests/` yet; CI runs `python -m pytest tests` once one exists):

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install "jarvis-command-sdk @ git+https://github.com/alexberardi/jarvis-command-sdk@main" \
  "jarvis-developer-toolkit @ git+https://github.com/alexberardi/jarvis-developer-toolkit@main" pytest
jdt test . --install-deps
```

**Merging.** An agent may squash-merge **its own** PR into `main` once CI has passed, then check main's CI after the merge. **Hold for the user's review** instead, with a note saying why, when the PR:

- changes `jarvis_package.yaml` at all (its `version`, `secrets`, `packages`, `apt_packages` and `post_install` are what reach users' nodes);
- touches `*_scripts/**` or `*_shared/**` (OAuth and token code lives there);
- touches `.github/**`;
- deletes, renames or weakens a test;
- adds a secret (`JarvisSecret`) or a new outbound host, or is marked "needs review" by its author.

**Releases are the user's.** Tagging `v<version>` and submitting to Pantry are always done by the user: never push a tag or submit. Never merge someone else's PR, never force-push to `main`.
