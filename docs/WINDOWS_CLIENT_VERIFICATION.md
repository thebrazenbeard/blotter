# Source qualification, 2026-11-07

The repair reuses the credential and integrity candidate from PR #34 at
`23665b8bb59eb1b63d17985ec148512932ceed93`. No production deployment is included.

| Actual Windows verification | Result |
| --- | --- |
| Full suite, Python 3.12.10 | 168 run; 157 passed; 11 skipped; zero failures |
| Full suite, Python 3.14.5 | 168 run; 157 passed; 11 skipped; zero failures |
| Retained actual HTTP/process verifier, Python 3.14.5 | 20 checks passed |
| Independent hostile tests | 8 passed |
| Independent MCP suite after test-fixture correction | 25 run; 24 passed; one POSIX skip |

The eleven full-suite skips comprise ten POSIX-only cases and a local TLS
fixture requiring an unavailable OpenSSL executable. They are not counted as
passes. Cross-host transport and production recovery are not qualified here.

The real-process verifier checks anonymous read rejection, bad bearer and
wrong-actor write rejection, two actual client events and readback, controlled
stop, a new server process using the same synthetic ledger, exact-ID replay
without duplicates, changed-body ID collision rejection, and unchanged hash
chain/integrity. This proves controlled process-restart behavior for that test
lane, not abrupt-crash, power-loss, boot recovery or universal capture.

## Preserved negative results

- The new positive Windows file-input test failed on the predecessor probe,
  which denied the file before calling the available verified reader.
- Initial combined suites on both interpreters failed an existing reparse-race
  fixture: PowerShell could not create the replacement symlink, so the intended
  race never occurred. The test now precreates a synthetic symlink through
  Python's supported Windows path, then moves it at the file-open boundary.
  The exact opened-handle rejection oracle remains required and passed.
- Initial restricted local-socket runs failed at the execution boundary. The
  reported integration results used authorized loopback-capable execution.
- Linux/Python 3.10 CI exposed an old simulated-Windows turn test attempting
  native Windows path construction. That test now supplies an explicit
  verified-reader denial and retains its required `PermissionError` assertion;
  actual Windows private-file acceptance/rejection remains independently tested.

Five distinct native specialists investigated authentication, runtime,
integration mapping, durability and hostile review. The hostile reviewer found
the ineffective race fixture and independently verified the correction. No
unresolved material defect was found in the narrow source repair; this is not a
general security certification of the service or its deployment.

Detailed live audit evidence is retained locally. It is deliberately absent
from this public source package. [Repeatable commands and limits](WINDOWS_CLIENT_REPAIR.md)
describe the implemented interface without publishing infrastructure records.
