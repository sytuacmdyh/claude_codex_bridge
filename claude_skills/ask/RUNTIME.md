# Async Ask

Use this only for `/ask`.

Always send `MESSAGE` through the `<<'EOF' ... EOF` heredoc below. No other form is allowed.

```bash
command ask "$TARGET" <<'EOF'
$MESSAGE
EOF
```

- Sender is inferred from the current CCB workspace.
- `TARGET=all` broadcasts.
- After the command returns, immediately end the turn. Do not wait for a reply, do not run `pend` / `ping` / `watch`, do not poll.
