# Contributing to Tally Connector

Thank you for helping. Bug reports, feature requests and pull requests are all
welcome.

## Issues

- Search existing issues before opening a new one.
- One issue per problem, with steps to reproduce and the versions you use.
- This repository is public. Remove real company names, amounts, GSTINs, bank
  details and document numbers from anything you post.
- Security problems go through private reporting, not issues. See
  [SECURITY.md](../SECURITY.md).

## Pull requests

Every change reaches `develop` through a pull request, one feature or fix each.

1. Branch from `develop`, naming the branch after the change:
   `feat/short-name`, `fix/short-name`, `refactor/...`, `docs/...`, `chore/...`.
2. Write commits and the PR title as
   [conventional commits](https://www.conventionalcommits.org/):
   `feat: export cost centres`, `fix: keep bill references on a credit note`.
   The linter checks both, and pull requests are squash-merged, so the title
   becomes the commit on `develop`.
3. Keep the PR to one change. Formatting and refactoring go in their own PR.
4. Add or update tests. A fix should come with a test that fails without it.
5. Use demo data only: the fixtures' `Demo Traders Pvt Ltd`, `Acme Industries`,
   `Widget` and the like. Never a real company's names, figures or documents.
6. CI must pass: the server tests on a fresh site, and the linters.

## Development setup

```bash
bench get-app https://github.com/klyonix/tally_connector
bench --site your-site.local install-app tally_connector
bench --site your-site.local set-config allow_tests true

cd apps/tally_connector
pre-commit install          # ruff, prettier and eslint on each commit
```

Run the tests and the linters before you push:

```bash
bench --site your-site.local run-tests --app tally_connector
pre-commit run --all-files
```

The dashboard is a Vue app in `frontend/`, built on
[frappe-ui](https://github.com/frappe/frappe-ui):

```bash
cd frontend && yarn && yarn dev     # or yarn build
```

Code follows Frappe's conventions: Python is formatted by ruff with tabs, every
whitelisted method declares its HTTP methods and types its arguments, and
user-facing text goes through `_()`.

## License

By contributing, you agree that your contributions are licensed under the
[GNU AGPL v3](../LICENSE).
