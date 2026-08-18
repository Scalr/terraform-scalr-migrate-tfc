"""TFC/E to Scalr workspace migration library.

Deliberately does not eagerly import cli/service/args here: those pull in
third-party dependencies (e.g. `packaging`) that only the full migrator
needs. Lighter-weight consumers of this package - like tfc-discovery's
discover.py, which only needs clients/console/errors/discovery - would
otherwise be forced to have `packaging` installed just to import the
package at all. Nothing in this repo relies on these top-level re-exports;
everywhere imports the specific submodule directly
(e.g. `from scalr_tfc_migrate.cli import main`).
"""
