"""Fresh CPU process; read-only DB, disabled external access, bounded resources."""
import sys
from datetime import date, datetime
import duckdb
from .util import read_json, write_json
from .execution import validate_sql


def main():
    request = read_json(sys.argv[1])
    try:
        allowed = set(request["allowed_tables"])
        for sql in request["queries"]:
            validate_sql(sql, allowed)
        con = duckdb.connect(request["database"], read_only=True, config={
            "enable_external_access": "false", "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false", "memory_limit": request["memory_limit"],
            "threads": str(request["threads"]), "max_temp_directory_size": "0B"})
        try:
            bindings = {name.casefold(): value for name, value in request["bindings"].items()}
            def cell(v):
                if isinstance(v, datetime):
                    return v.isoformat(sep=" ")
                if isinstance(v, date):
                    return v.isoformat()
                return v
            results = []
            for sql in request["queries"]:
                statement, = con.extract_statements(sql)
                unknown = {name for name in statement.named_parameters if name.casefold() not in bindings}
                if unknown:
                    raise ValueError(f"Unknown SQL parameters: {sorted(unknown)}")
                selected = {name: bindings[name.casefold()] for name in statement.named_parameters}
                if any(value is None for value in selected.values()):
                    raise ValueError("SQL uses an unresolved time bound")
                cursor = con.execute(statement, selected)
                columns = [c[0] for c in cursor.description]
                rows = cursor.fetchmany(request["max_rows"] + 1)
                if len(rows) > request["max_rows"]:
                    raise ValueError(f"Result exceeded {request['max_rows']} rows; aggregate or rank in SQL before returning")
                results.append({"columns": columns, "rows": [[cell(v) for v in row] for row in rows]})
            write_json(request["response"], {"results": results})
        finally:
            con.close()
    except Exception as e:
        write_json(request["response"], {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()

