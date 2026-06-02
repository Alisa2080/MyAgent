# Cron CLI Delivery Service Env Design

## Goal

Make manual cron CLI commands that synchronously process delivery use the same default credentials as the installed cron service. A user who stores Feishu credentials with `agent cron service env set` should be able to run `agent cron test-delivery`, `agent cron tick`, or `agent cron run` from a shell that does not export `FEISHU_APP_ID` or `FEISHU_APP_SECRET`.

The immediate bug is that `cron test-delivery --target feishu:<chat_id>` fails when the current shell lacks Feishu variables, even though the service env file contains them and the installed service can send successfully.

## Scope

Cover CLI commands that perform delivery processing in the current Python process:

- `cron test-delivery`, which enqueues a synthetic event and immediately calls delivery processing.
- `cron tick`, which runs scheduler work and delivery maintenance.
- `cron run`, which now uses the real scheduler/run/delivery path.

Do not change pure query or state-reset commands:

- `cron list`, `cron runs`, `cron deliveries`, and `cron logs` do not need delivery credentials.
- `cron retry-delivery` only resets delivery state and should not load credentials unless it later gains immediate send behavior.
- `cron serve` is already the long-running runtime entry point. User service managers load its environment through the installed unit or plist.

## Environment Model

Add a small cron CLI runtime environment helper, used only around commands in scope.

The helper reads `cron.service_env.read_service_env()` and overlays those values onto `os.environ` for the duration of the command. The current shell wins over the service env file, so explicit operator overrides remain possible:

1. Start with values from `service.env`.
2. Overlay current `os.environ`.
3. Temporarily set the effective values for keys known in `service.env`.
4. Restore the original process environment when the command returns or raises.

This keeps adapter code simple. Feishu and future platform adapters can continue reading credentials from `os.environ`, while cron CLI commands supply the same defaults that the service runtime uses.

## Security

The helper must never print raw environment values. Existing command output should continue to show delivery event ids, targets, statuses, and errors only. If diagnostics mention service env, they should mention missing keys by name, not values.

The helper should restore `os.environ` after command execution. This matters for tests and for any future command dispatcher that invokes multiple commands in one process.

## Error Handling

If `service.env` is absent, the commands should behave exactly as they do today and rely on the current shell environment.

If `service.env` is unreadable or malformed according to existing parsing rules, commands should not crash before reaching the existing delivery error path. The current `read_service_env()` behavior ignores malformed lines and returns parsed valid key/value pairs, so the helper can use it directly.

If both service env and shell env lack required Feishu credentials, the Feishu adapter should continue returning the existing missing environment variable error.

## Testing

Add focused regression coverage for the environment overlay:

- `cron test-delivery` succeeds when `FEISHU_APP_ID` and `FEISHU_APP_SECRET` exist only in `service.env`.
- Shell values override service env values when both are present.
- Command output does not include raw Feishu credential values.
- At least one scheduler path command, preferably `cron run` or `cron tick`, processes Feishu delivery using credentials from `service.env`.
- The helper restores `os.environ` after command completion.

Tests should use fake gateway adapters or fake HTTP senders so they do not call real Feishu APIs.

## Acceptance

After implementation, this command sequence should work without exporting Feishu credentials in the shell:

```bash
agent cron service env set FEISHU_APP_ID APP_ID_VALUE
agent cron service env set FEISHU_APP_SECRET APP_SECRET_VALUE
unset FEISHU_APP_ID FEISHU_APP_SECRET
agent cron test-delivery --target feishu:oc_xxx
```

The expected result is a delivered test event when the credentials and target are valid. `cron tick` and `cron run` should use the same environment behavior for Feishu delivery.
