"""Fresh CPU process; read-only DB, disabled external access, bounded resources."""
import sys
from datetime import date, datetime
import duckdb
from .util import read_json, write_json
from .execution import validate_sql


def main():
    request = read_json(sys.argv[1])
    try:
        validate_sql(request["sql"], set(request["allowed_tables"]))
        con = duckdb.connect(request["database"], read_only=True, config={
            "enable_external_access": "false", "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false", "memory_limit": request["memory_limit"],
            "threads": str(request["threads"]), "max_temp_directory_size": "0B"})
        try:
            cursor = con.execute(request["sql"], request["bindings"])
            columns = [c[0] for c in cursor.description]
            rows = cursor.fetchmany(request["max_rows"] + 1)
            if len(rows) > request["max_rows"]:
                raise ValueError("Result row cap exceeded; refine the query")
            def cell(v):
                if isinstance(v, datetime):
                    return v.isoformat(sep=" ")
                if isinstance(v, date):
                    return v.isoformat()
                return v
            write_json(request["response"], {"columns": columns, "rows": [[cell(v) for v in row] for row in rows]})
        finally:
            con.close()
    except Exception as e:
        write_json(request["response"], {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()

