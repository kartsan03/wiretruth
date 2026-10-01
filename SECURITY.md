# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/kartsan03/wiretruth/security/advisories/new). Don't open a
public issue.

You'll get a first response within 7 days.

## Scope

In scope:

- Secrets or account identifiers leaking into fixtures, results or workflow logs.
- The recorder persisting request headers or credentials.
- GitHub Actions workflows granting more access than they need, or running untrusted input.
- The replay server binding to anything other than the loopback interface.

Out of scope:

- Bugs in the libraries being tested. Report those to the library. If a finding looks security-relevant for a
  tested library, we use that library's private advisory channel, and the matrix cell shows "under disclosure"
  until a fix ships or 90 days pass.

## Supported versions

Only the latest release gets fixes.
