[日本語](../local-tools.md)

# Windows local tools

The Relay can expose a small allow-list of Windows-local actions to Foundry
Realtime Function Calling. It does not expose a command shell.

## Browser tool

Enable it in `relay/.env`:

```dotenv
LOCAL_BROWSER_TOOL_ENABLED=true
LOCAL_BROWSER_ALLOWED_DOMAINS=microsoft.com,github.com,localhost
```

The registered function is:

```json
{
  "name": "open_browser_url",
  "arguments": {
    "url": "https://learn.microsoft.com/"
  }
}
```

The Relay validates the URL and sends it to the loopback-only Local Manager.
The Manager repeats the same validation, then hands the URL to the interactive
Windows Explorer session so it opens in the user's default browser and profile.
Both the Manager and Relay must be running. Only HTTP and HTTPS are accepted.
URLs containing credentials are rejected. When an allow-list is configured,
exact domains and their subdomains are accepted.

## Adding another local action

Add each capability as a named implementation in
`relay/app/tools/local_actions.py`:

1. Add a narrow JSON schema to `tool_definitions()`.
2. Add the name to `supports()`.
3. Validate every argument before causing a side effect.
4. Return a small JSON-serializable result.
5. Add success, rejection, and disabled-state tests.
6. Add a separate environment switch; keep the default disabled.

Do not add a generic `run_command`, `powershell`, `open_file`, or arbitrary
Python evaluation function. Prefer purpose-built actions such as:

- open an allow-listed dashboard
- show a fixed local status page
- control a specific home-automation endpoint
- create a reminder through a dedicated API

Actions that send messages, purchase items, delete data, change security
settings, or expose local files require an explicit confirmation design before
they are made available to the model.
