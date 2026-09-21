# Contributing

## Running the tests

```bash
python3 test_imgintake.py        # pure transform tests (hermetic)
python3 test_security.py         # security regressions, one per audit finding
python3 test_bootstrap_unit.py   # /img/token gate with a mocked upstream
npm i && npm run test:render     # userscript renderer/drop logic in jsdom

# live (optional, needs a bridge on localhost:8081)
BASE=http://127.0.0.1:8081 ZEN_DIR=$HOME/Documents ./test_imghost.sh
```

Please keep every test hermetic: temp `IMAGE_DIR`/`ZEN_CONFIG_DIR` before
importing the bridge, no reliance on `~/Documents`, no LAN IPs or tunnel URLs.

## House rules

- Never commit `*.local.sh`, tokens, logs, or a real hostname/domain.
  `start-zen-proxy.local.sh` is gitignored for exactly that reason.
- Security-relevant behaviour is covered by `test_security.py` — add a check
  there when you fix or change something in that area.
- Bump `@version` in the userscript header for any behaviour change; devices
  update from the GitHub raw URL.
