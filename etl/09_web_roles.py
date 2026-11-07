"""Create the read-only database roles the web services use (M5), then apply their grants.

    tiles_reader  -> pg_tileserv (its grants are the list of published map layers)
    api_reader    -> the FastAPI app in api/

Passwords come from TILES_DB_PASSWORD and API_DB_PASSWORD in .env. If one is missing, a random
password is generated and appended to .env (which git ignores); it is never printed.

Steps 08 and network_qc recreate their tables, which drops the grants, so this runs at the end
of run_all.py and again every time scripts/start.ps1 starts the services. It is safe to rerun.
"""
import os
import secrets

from psycopg import sql as pgsql

from common import REPO, connect, log, main_guard, sql

ROLES = {"tiles_reader": "TILES_DB_PASSWORD", "api_reader": "API_DB_PASSWORD"}
GRANTS = REPO / "db" / "serving" / "01_web_roles.sql"


def password(var: str) -> str:
    value = os.getenv(var)
    if value:
        return value
    value = secrets.token_urlsafe(24)
    env = REPO / ".env"
    text = env.read_text() if env.exists() else ""
    with open(env, "a") as f:
        f.write(("" if text.endswith("\n") or not text else "\n") + f"{var}={value}\n")
    os.environ[var] = value
    log(f"generated {var} and saved it to .env")
    return value


def main() -> None:
    conn = connect()
    for role, var in ROLES.items():
        exists = sql(conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        verb = "ALTER" if exists else "CREATE"
        sql(conn, pgsql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}")
            .format(pgsql.SQL(verb), pgsql.Identifier(role), pgsql.Literal(password(var))))
        log(f"{'created' if verb == 'CREATE' else 'updated'} role {role}")
    sql(conn, pgsql.SQL("GRANT CONNECT ON DATABASE {} TO tiles_reader, api_reader")
        .format(pgsql.Identifier(conn.info.dbname)))
    sql(conn, GRANTS.read_text())
    layers = sql(conn, """
        SELECT count(*) FROM information_schema.role_table_grants
        WHERE grantee = 'tiles_reader' AND privilege_type = 'SELECT'""")[0][0]
    log(f"grants applied; tiles_reader can read {layers} tables")


if __name__ == "__main__":
    main_guard(main)
