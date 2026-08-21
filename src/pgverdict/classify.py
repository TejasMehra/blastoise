"""Classification of parsed statements into the pgverdict IR.

Every function here works purely on the pglast (libpg_query) parse tree —
no regex, no string matching against the SQL text. The SQL text is only
carried through for reporting and for re-parsing DO block bodies.
"""

from __future__ import annotations

import pglast.printers  # noqa: F401  (import registers the deparse printers)
from pglast import ast, enums, parse_plpgsql
from pglast.error import Error as PglastError
from pglast.parser import parse_sql
from pglast.stream import RawStream
from pglast.visitors import Ancestor, Visitor

from pgverdict.ir import (
    AlterTableAction,
    AlterTableActionKind,
    CreateTableAsDetails,
    CreateTableDetails,
    DefaultInfo,
    DmlDetails,
    DoBlockDetails,
    DropDetails,
    IndexDetails,
    InsertDetails,
    LockDetails,
    ParsedStatement,
    QualifiedName,
    ReindexDetails,
    RenameDetails,
    SetDetails,
    SourceSpan,
    StatementKind,
    TransactionDetails,
    TruncateDetails,
    Volatility,
)
from pgverdict.volatility import expression_volatility, unknown_function_keys

_SERIAL_TYPE_NAMES = frozenset(
    {"serial", "serial2", "serial4", "serial8", "bigserial", "smallserial"}
)

_LOCK_MODE_NAMES = {
    1: "ACCESS SHARE",
    2: "ROW SHARE",
    3: "ROW EXCLUSIVE",
    4: "SHARE UPDATE EXCLUSIVE",
    5: "SHARE",
    6: "SHARE ROW EXCLUSIVE",
    7: "EXCLUSIVE",
    8: "ACCESS EXCLUSIVE",
}


def _deparse(node: ast.Node | None) -> str | None:
    if node is None:
        return None
    try:
        return str(RawStream()(node))
    except Exception:  # deparse is best-effort decoration only
        return None


def _qname(rangevar: ast.RangeVar | None) -> QualifiedName | None:
    if rangevar is None or rangevar.relname is None:
        return None
    return QualifiedName(name=rangevar.relname, schema=rangevar.schemaname)


def _targets(*rangevars: ast.RangeVar | None) -> tuple[QualifiedName, ...]:
    return tuple(name for name in (_qname(rv) for rv in rangevars) if name is not None)


def _strings(items: object) -> list[str]:
    if not isinstance(items, tuple | list):
        return []
    return [item.sval for item in items if isinstance(item, ast.String) and item.sval is not None]


def _qname_from_strings(parts: list[str]) -> QualifiedName | None:
    if not parts:
        return None
    if len(parts) == 1:
        return QualifiedName(name=parts[0])
    return QualifiedName(name=parts[-1], schema=parts[-2])


def _is_serial_type(type_name: ast.TypeName | None) -> bool:
    if type_name is None:
        return False
    # Postgres expands serial pseudo-types only for unqualified names;
    # myschema.serial is looked up as a real type.
    names = _strings(type_name.names)
    return len(names) == 1 and names[0].lower() in _SERIAL_TYPE_NAMES


def _defelem_enabled(elem: ast.DefElem) -> bool:
    """Boolean utility-option semantics (defGetBoolean): bare option = true."""
    arg = elem.arg
    if arg is None:
        return True
    if isinstance(arg, ast.String):
        return (arg.sval or "").lower() in {"true", "on", "yes", "1"}
    if isinstance(arg, ast.Integer):
        return arg.ival != 0
    if isinstance(arg, ast.Boolean):
        return bool(arg.boolval)
    return True


def _is_null_constant(expr: ast.Node | None) -> bool:
    while isinstance(expr, ast.TypeCast):
        expr = expr.arg
    return isinstance(expr, ast.A_Const) and bool(expr.isnull)


# --------------------------------------------------------------------------
# DML backfill shape (UPDATE/DELETE batching idioms)
# --------------------------------------------------------------------------


class _BatchSignalFinder(Visitor):
    """Find LIMIT-bounded subqueries and ctid references anywhere in a tree."""

    def __init__(self) -> None:
        self.limited = False
        self.ctid = False

    def visit_SelectStmt(self, ancestors: Ancestor, node: ast.SelectStmt) -> None:
        if node.limitCount is not None:
            self.limited = True

    def visit_ColumnRef(self, ancestors: Ancestor, node: ast.ColumnRef) -> None:
        fields = [f for f in node.fields or () if isinstance(f, ast.String)]
        if fields and fields[-1].sval == "ctid":
            self.ctid = True


_LOWER_BOUND_OPS = frozenset({">", ">="})
_UPPER_BOUND_OPS = frozenset({"<", "<="})


def _column_of(expr: ast.Node | None) -> str | None:
    if not isinstance(expr, ast.ColumnRef):
        return None
    fields = [f.sval for f in expr.fields or () if isinstance(f, ast.String) and f.sval]
    return fields[-1] if fields else None


def _has_key_window(where: ast.Node | None) -> bool:
    """True when top-level AND terms bound the same column from both sides.

    Recognizes ``col >= x AND col < y`` windows and ``col BETWEEN a AND b`` —
    the id-windowing batching idiom. A single one-sided bound (e.g. a
    retention cutoff ``created_at < now() - ...``) is not a window.
    """
    lower: set[str] = set()
    upper: set[str] = set()
    between = False

    def collect(expr: ast.Node | None) -> None:
        nonlocal between
        if isinstance(expr, ast.BoolExpr) and expr.boolop is enums.BoolExprType.AND_EXPR:
            for arg in expr.args or ():
                if isinstance(arg, ast.Node):
                    collect(arg)
            return
        if not isinstance(expr, ast.A_Expr):
            return
        names = [s.sval for s in expr.name or () if isinstance(s, ast.String) and s.sval]
        op = names[0] if len(names) == 1 else None
        if expr.kind is enums.A_Expr_Kind.AEXPR_BETWEEN and _column_of(expr.lexpr):
            between = True
        elif op in _LOWER_BOUND_OPS or op in _UPPER_BOUND_OPS:
            column = _column_of(expr.lexpr)
            reversed_operands = False
            if column is None:
                column = _column_of(expr.rexpr)
                reversed_operands = True
            if column is None:
                return
            is_lower = op in _LOWER_BOUND_OPS
            if reversed_operands:  # x < col means col > x
                is_lower = not is_lower
            (lower if is_lower else upper).add(column)

    collect(where)
    return between or bool(lower & upper)


def _classify_dml(
    node: ast.UpdateStmt | ast.DeleteStmt, sql: str, span: SourceSpan
) -> ParsedStatement:
    is_update = isinstance(node, ast.UpdateStmt)
    has_where = node.whereClause is not None
    signals: list[str] = []
    if has_where:
        finder = _BatchSignalFinder()
        finder(node)
        if finder.limited:
            signals.append("limit")
        if finder.ctid:
            signals.append("ctid")
        if _has_key_window(node.whereClause):
            signals.append("key_window")

    if not has_where:
        kind = (
            StatementKind.UPDATE_WITHOUT_WHERE
            if is_update
            else StatementKind.DELETE_WITHOUT_WHERE
        )
    elif signals:
        kind = StatementKind.UPDATE_BATCHED if is_update else StatementKind.DELETE_BATCHED
    else:
        kind = StatementKind.UPDATE if is_update else StatementKind.DELETE
    return ParsedStatement(
        kind=kind,
        sql=sql,
        span=span,
        node_tag=type(node).__name__,
        targets=_targets(node.relation),
        details=DmlDetails(has_where=has_where, batch_signals=tuple(signals)),
    )


# --------------------------------------------------------------------------
# Catalog existence guards inside DO blocks
# --------------------------------------------------------------------------

_CATALOG_RELATIONS = frozenset(
    {
        "pg_class", "pg_attribute", "pg_constraint", "pg_type", "pg_enum",
        "pg_index", "pg_indexes", "pg_tables", "pg_views", "pg_matviews",
        "pg_sequences", "pg_namespace", "pg_proc", "pg_trigger", "pg_policies",
        "pg_extension", "pg_roles", "pg_attrdef", "pg_am", "pg_database",
        "pg_settings", "pg_publication", "pg_subscription",
    }
)

_EXISTENCE_PROBE_FUNCTIONS = frozenset(
    {"to_regclass", "to_regtype", "to_regproc", "to_regprocedure", "to_regnamespace",
     "to_regrole", "to_regoper", "to_regoperator"}
)


class _CatalogProbeFinder(Visitor):
    """Detect references to system catalogs or to_reg*() probes."""

    def __init__(self) -> None:
        self.found = False

    def visit_RangeVar(self, ancestors: Ancestor, node: ast.RangeVar) -> None:
        if node.schemaname in ("information_schema", "pg_catalog") or (
            node.schemaname is None and node.relname in _CATALOG_RELATIONS
        ):
            self.found = True

    def visit_FuncCall(self, ancestors: Ancestor, node: ast.FuncCall) -> None:
        names = [
            s.sval for s in node.funcname or () if isinstance(s, ast.String) and s.sval
        ]
        if names and names[-1].lower() in _EXISTENCE_PROBE_FUNCTIONS:
            self.found = True


def _condition_probes_catalog(condition: str) -> bool:
    """Parse a plpgsql IF condition and look for catalog existence checks."""
    try:
        raw = parse_sql(f"SELECT {condition}")
    except PglastError:
        return False
    finder = _CatalogProbeFinder()
    for stmt in raw:
        if stmt.stmt is not None:
            finder(stmt.stmt)
    return finder.found


def _default_info(expr: ast.Node | None) -> DefaultInfo:
    volatility = expression_volatility(expr)
    # Record the names behind an UNKNOWN so a live pg_proc lookup can decide
    # it later; when the answer is already decided there is nothing to ask.
    unknown = unknown_function_keys(expr) if volatility is Volatility.UNKNOWN else ()
    return DefaultInfo(
        volatility=volatility, expression=_deparse(expr), unknown_functions=unknown
    )


# --------------------------------------------------------------------------
# ALTER TABLE subcommands
# --------------------------------------------------------------------------

_INLINE_CONSTRAINT_LABELS = {
    enums.ConstrType.CONSTR_PRIMARY: "primary_key",
    enums.ConstrType.CONSTR_UNIQUE: "unique",
    enums.ConstrType.CONSTR_FOREIGN: "references",
    enums.ConstrType.CONSTR_CHECK: "check",
}


def _add_column_action(cmd: ast.AlterTableCmd) -> AlterTableAction:
    column_def = cmd.def_ if isinstance(cmd.def_, ast.ColumnDef) else None
    if column_def is None:
        return AlterTableAction(kind=AlterTableActionKind.OTHER, detail="AT_AddColumn")

    not_null = False
    default_expr: ast.Node | None = None
    has_default = False
    identity = False
    generated_stored = False
    generated_virtual = False
    inline: list[str] = []
    referenced_table: QualifiedName | None = None
    for con in column_def.constraints or ():
        if not isinstance(con, ast.Constraint):
            continue
        contype = con.contype
        if contype is enums.ConstrType.CONSTR_FOREIGN and referenced_table is None:
            referenced_table = _qname(con.pktable)
        if contype is enums.ConstrType.CONSTR_NOTNULL:
            not_null = True
        elif contype is enums.ConstrType.CONSTR_DEFAULT:
            has_default = True
            default_expr = con.raw_expr
        elif contype is enums.ConstrType.CONSTR_IDENTITY:
            identity = True
        elif contype is enums.ConstrType.CONSTR_GENERATED:
            if con.generated_kind == "v":
                generated_virtual = True
            else:
                generated_stored = True
        elif contype in _INLINE_CONSTRAINT_LABELS:
            inline.append(_INLINE_CONSTRAINT_LABELS[contype])

    default: DefaultInfo | None = None
    if generated_stored:
        kind = AlterTableActionKind.ADD_COLUMN_GENERATED_STORED
    elif generated_virtual:
        kind = AlterTableActionKind.ADD_COLUMN_GENERATED_VIRTUAL
    elif identity:
        kind = AlterTableActionKind.ADD_COLUMN_IDENTITY
    elif _is_serial_type(column_def.typeName):
        kind = AlterTableActionKind.ADD_COLUMN_SERIAL
    elif has_default:
        default = _default_info(default_expr)
        if not_null and _is_null_constant(default_expr):
            # DEFAULT NULL provides no fill value: on a non-empty table this
            # fails exactly like NOT NULL without a default.
            kind = AlterTableActionKind.ADD_COLUMN_NOT_NULL_NO_DEFAULT
        elif default.volatility is Volatility.VOLATILE:
            kind = AlterTableActionKind.ADD_COLUMN_DEFAULT_VOLATILE
        elif default.volatility is Volatility.UNKNOWN:
            kind = AlterTableActionKind.ADD_COLUMN_DEFAULT_UNKNOWN_VOLATILITY
        else:
            kind = AlterTableActionKind.ADD_COLUMN_DEFAULT_NONVOLATILE
    elif not_null:
        kind = AlterTableActionKind.ADD_COLUMN_NOT_NULL_NO_DEFAULT
    else:
        kind = AlterTableActionKind.ADD_COLUMN

    return AlterTableAction(
        kind=kind,
        column=column_def.colname,
        column_type=_deparse(column_def.typeName),
        default=default,
        not_null=not_null,
        inline_constraints=tuple(inline),
        referenced_table=referenced_table,
        missing_ok=bool(cmd.missing_ok),
    )


def _add_constraint_action(cmd: ast.AlterTableCmd) -> AlterTableAction:
    con = cmd.def_ if isinstance(cmd.def_, ast.Constraint) else None
    if con is None:
        return AlterTableAction(kind=AlterTableActionKind.OTHER, detail="AT_AddConstraint")

    not_valid = bool(con.skip_validation)
    using_index = con.indexname
    contype = con.contype
    column: str | None = None
    referenced_table: QualifiedName | None = None

    if contype is enums.ConstrType.CONSTR_PRIMARY:
        kind = (
            AlterTableActionKind.ADD_PRIMARY_KEY_USING_INDEX
            if using_index
            else AlterTableActionKind.ADD_PRIMARY_KEY
        )
    elif contype is enums.ConstrType.CONSTR_UNIQUE:
        kind = (
            AlterTableActionKind.ADD_UNIQUE_USING_INDEX
            if using_index
            else AlterTableActionKind.ADD_UNIQUE
        )
    elif contype is enums.ConstrType.CONSTR_FOREIGN:
        kind = (
            AlterTableActionKind.ADD_FOREIGN_KEY_NOT_VALID
            if not_valid
            else AlterTableActionKind.ADD_FOREIGN_KEY
        )
        referenced_table = _qname(con.pktable)
    elif contype is enums.ConstrType.CONSTR_CHECK:
        kind = (
            AlterTableActionKind.ADD_CHECK_NOT_VALID
            if not_valid
            else AlterTableActionKind.ADD_CHECK
        )
    elif contype is enums.ConstrType.CONSTR_NOTNULL:
        kind = (
            AlterTableActionKind.ADD_NOT_NULL_CONSTRAINT_NOT_VALID
            if not_valid
            else AlterTableActionKind.ADD_NOT_NULL_CONSTRAINT
        )
        keys = _strings(con.keys)
        column = keys[0] if keys else None
    elif contype is enums.ConstrType.CONSTR_EXCLUSION:
        kind = AlterTableActionKind.ADD_EXCLUSION
    else:
        kind = AlterTableActionKind.ADD_CONSTRAINT_OTHER

    detail: str | None = None
    if kind is AlterTableActionKind.ADD_CONSTRAINT_OTHER:
        detail = contype.name if contype is not None else "unknown"
    return AlterTableAction(
        kind=kind,
        column=column,
        constraint_name=con.conname,
        not_valid=not_valid,
        using_index=using_index,
        referenced_table=referenced_table,
        detail=detail,
    )


def _partition_action(
    cmd: ast.AlterTableCmd, kind: AlterTableActionKind | None = None
) -> AlterTableAction:
    partition_cmd = cmd.def_ if isinstance(cmd.def_, ast.PartitionCmd) else None
    partition = _qname(partition_cmd.name) if partition_cmd is not None else None
    if kind is None:
        concurrent = bool(partition_cmd.concurrent) if partition_cmd is not None else False
        kind = (
            AlterTableActionKind.DETACH_PARTITION_CONCURRENTLY
            if concurrent
            else AlterTableActionKind.DETACH_PARTITION
        )
    return AlterTableAction(kind=kind, partition=partition)


_AT = enums.AlterTableType

# Subcommands that carry no extra information beyond the kind (plus the
# generic name/cascade/missing_ok fields handled in _alter_table_action).
_SIMPLE_ALTER_KINDS: dict[enums.AlterTableType, AlterTableActionKind] = {
    _AT.AT_DropColumn: AlterTableActionKind.DROP_COLUMN,
    _AT.AT_SetNotNull: AlterTableActionKind.SET_NOT_NULL,
    _AT.AT_DropNotNull: AlterTableActionKind.DROP_NOT_NULL,
    _AT.AT_ValidateConstraint: AlterTableActionKind.VALIDATE_CONSTRAINT,
    _AT.AT_DropConstraint: AlterTableActionKind.DROP_CONSTRAINT,
    _AT.AT_SetStatistics: AlterTableActionKind.SET_STATISTICS,
    _AT.AT_SetStorage: AlterTableActionKind.SET_STORAGE,
    _AT.AT_SetCompression: AlterTableActionKind.SET_COMPRESSION,
    _AT.AT_SetOptions: AlterTableActionKind.SET_COLUMN_OPTIONS,
    _AT.AT_ResetOptions: AlterTableActionKind.RESET_COLUMN_OPTIONS,
    _AT.AT_SetExpression: AlterTableActionKind.SET_EXPRESSION,
    _AT.AT_DropExpression: AlterTableActionKind.DROP_EXPRESSION,
    _AT.AT_AddIdentity: AlterTableActionKind.ADD_IDENTITY,
    _AT.AT_SetIdentity: AlterTableActionKind.SET_IDENTITY,
    _AT.AT_DropIdentity: AlterTableActionKind.DROP_IDENTITY,
    _AT.AT_SetRelOptions: AlterTableActionKind.SET_STORAGE_PARAMS,
    _AT.AT_ResetRelOptions: AlterTableActionKind.RESET_STORAGE_PARAMS,
    _AT.AT_ReplaceRelOptions: AlterTableActionKind.REPLACE_STORAGE_PARAMS,
    _AT.AT_SetLogged: AlterTableActionKind.SET_LOGGED,
    _AT.AT_SetUnLogged: AlterTableActionKind.SET_UNLOGGED,
    _AT.AT_SetTableSpace: AlterTableActionKind.SET_TABLESPACE,
    _AT.AT_SetAccessMethod: AlterTableActionKind.SET_ACCESS_METHOD,
    _AT.AT_EnableRowSecurity: AlterTableActionKind.ENABLE_ROW_SECURITY,
    _AT.AT_DisableRowSecurity: AlterTableActionKind.DISABLE_ROW_SECURITY,
    _AT.AT_ForceRowSecurity: AlterTableActionKind.FORCE_ROW_SECURITY,
    _AT.AT_NoForceRowSecurity: AlterTableActionKind.NO_FORCE_ROW_SECURITY,
    _AT.AT_ClusterOn: AlterTableActionKind.CLUSTER_ON,
    _AT.AT_DropCluster: AlterTableActionKind.DROP_CLUSTER,
    _AT.AT_ChangeOwner: AlterTableActionKind.CHANGE_OWNER,
    _AT.AT_AddInherit: AlterTableActionKind.INHERIT,
    _AT.AT_DropInherit: AlterTableActionKind.NO_INHERIT,
    _AT.AT_AddOf: AlterTableActionKind.OF_TYPE,
    _AT.AT_DropOf: AlterTableActionKind.NOT_OF,
    _AT.AT_ReplicaIdentity: AlterTableActionKind.REPLICA_IDENTITY,
}

# Trigger/rule toggles: (action kind, variant detail)
_TRIGGER_RULE_KINDS: dict[enums.AlterTableType, tuple[AlterTableActionKind, str | None]] = {
    _AT.AT_EnableTrig: (AlterTableActionKind.ENABLE_TRIGGER, None),
    _AT.AT_EnableAlwaysTrig: (AlterTableActionKind.ENABLE_TRIGGER, "always"),
    _AT.AT_EnableReplicaTrig: (AlterTableActionKind.ENABLE_TRIGGER, "replica"),
    _AT.AT_DisableTrig: (AlterTableActionKind.DISABLE_TRIGGER, None),
    _AT.AT_EnableTrigAll: (AlterTableActionKind.ENABLE_TRIGGER, "all"),
    _AT.AT_DisableTrigAll: (AlterTableActionKind.DISABLE_TRIGGER, "all"),
    _AT.AT_EnableTrigUser: (AlterTableActionKind.ENABLE_TRIGGER, "user"),
    _AT.AT_DisableTrigUser: (AlterTableActionKind.DISABLE_TRIGGER, "user"),
    _AT.AT_EnableRule: (AlterTableActionKind.ENABLE_RULE, None),
    _AT.AT_EnableAlwaysRule: (AlterTableActionKind.ENABLE_RULE, "always"),
    _AT.AT_EnableReplicaRule: (AlterTableActionKind.ENABLE_RULE, "replica"),
    _AT.AT_DisableRule: (AlterTableActionKind.DISABLE_RULE, None),
}

# Subcommands whose bare `name` field is a constraint name, not a column.
_CONSTRAINT_NAME_KINDS = frozenset(
    {AlterTableActionKind.VALIDATE_CONSTRAINT, AlterTableActionKind.DROP_CONSTRAINT}
)

# Subcommands whose bare `name` field is neither a column nor a constraint
# (trigger/rule toggles are handled separately via _TRIGGER_RULE_KINDS).
_NON_COLUMN_NAME_KINDS = frozenset(
    {
        AlterTableActionKind.SET_TABLESPACE,
        AlterTableActionKind.SET_ACCESS_METHOD,
        AlterTableActionKind.CLUSTER_ON,
    }
)


def _alter_table_action(cmd: ast.AlterTableCmd) -> AlterTableAction:
    subtype = cmd.subtype
    cascade = cmd.behavior is enums.DropBehavior.DROP_CASCADE
    missing_ok = bool(cmd.missing_ok)

    if subtype is _AT.AT_AddColumn:
        return _add_column_action(cmd)
    if subtype is _AT.AT_AddConstraint:
        return _add_constraint_action(cmd)
    if subtype is _AT.AT_ColumnDefault:
        if cmd.def_ is None:
            return AlterTableAction(
                kind=AlterTableActionKind.DROP_COLUMN_DEFAULT, column=cmd.name
            )
        return AlterTableAction(
            kind=AlterTableActionKind.SET_COLUMN_DEFAULT,
            column=cmd.name,
            default=_default_info(cmd.def_),
        )
    if subtype is _AT.AT_AlterColumnType:
        column_def = cmd.def_ if isinstance(cmd.def_, ast.ColumnDef) else None
        return AlterTableAction(
            kind=AlterTableActionKind.ALTER_COLUMN_TYPE,
            column=cmd.name,
            column_type=_deparse(column_def.typeName) if column_def is not None else None,
            has_using_expression=column_def is not None and column_def.raw_default is not None,
        )
    if subtype is _AT.AT_AlterConstraint:
        alter = cmd.def_ if isinstance(cmd.def_, ast.ATAlterConstraint) else None
        return AlterTableAction(
            kind=AlterTableActionKind.ALTER_CONSTRAINT,
            constraint_name=alter.conname if alter is not None else None,
        )
    if subtype is _AT.AT_AttachPartition:
        return _partition_action(cmd, AlterTableActionKind.ATTACH_PARTITION)
    if subtype is _AT.AT_DetachPartition:
        return _partition_action(cmd)
    if subtype is _AT.AT_DetachPartitionFinalize:
        return _partition_action(cmd, AlterTableActionKind.DETACH_PARTITION_FINALIZE)

    if subtype in _TRIGGER_RULE_KINDS:
        kind, variant = _TRIGGER_RULE_KINDS[subtype]
        return AlterTableAction(kind=kind, constraint_name=cmd.name, detail=variant)

    if subtype in _SIMPLE_ALTER_KINDS:
        kind = _SIMPLE_ALTER_KINDS[subtype]
        column = cmd.name
        constraint_name = None
        detail = None
        if kind in _CONSTRAINT_NAME_KINDS:
            column, constraint_name = None, cmd.name
        elif kind in _NON_COLUMN_NAME_KINDS:
            column, detail = None, cmd.name
        return AlterTableAction(
            kind=kind,
            column=column,
            constraint_name=constraint_name,
            cascade=cascade,
            missing_ok=missing_ok,
            detail=detail,
        )

    return AlterTableAction(
        kind=AlterTableActionKind.OTHER,
        column=cmd.name,
        detail=subtype.name if subtype is not None else None,
    )


_ALTER_RELATION_KINDS: dict[enums.ObjectType, StatementKind] = {
    enums.ObjectType.OBJECT_TABLE: StatementKind.ALTER_TABLE,
    enums.ObjectType.OBJECT_INDEX: StatementKind.ALTER_INDEX,
    enums.ObjectType.OBJECT_VIEW: StatementKind.ALTER_VIEW,
    enums.ObjectType.OBJECT_MATVIEW: StatementKind.ALTER_MATVIEW,
    enums.ObjectType.OBJECT_FOREIGN_TABLE: StatementKind.ALTER_FOREIGN_TABLE,
    enums.ObjectType.OBJECT_TYPE: StatementKind.ALTER_COMPOSITE_TYPE,
}


# --------------------------------------------------------------------------
# Statement-level classification
# --------------------------------------------------------------------------

_DROP_KINDS: dict[enums.ObjectType, StatementKind] = {
    enums.ObjectType.OBJECT_TABLE: StatementKind.DROP_TABLE,
    enums.ObjectType.OBJECT_VIEW: StatementKind.DROP_VIEW,
    enums.ObjectType.OBJECT_MATVIEW: StatementKind.DROP_MATVIEW,
    enums.ObjectType.OBJECT_SEQUENCE: StatementKind.DROP_SEQUENCE,
    enums.ObjectType.OBJECT_TYPE: StatementKind.DROP_TYPE,
    enums.ObjectType.OBJECT_DOMAIN: StatementKind.DROP_DOMAIN,
    enums.ObjectType.OBJECT_EXTENSION: StatementKind.DROP_EXTENSION,
    enums.ObjectType.OBJECT_TRIGGER: StatementKind.DROP_TRIGGER,
    enums.ObjectType.OBJECT_FUNCTION: StatementKind.DROP_FUNCTION,
    enums.ObjectType.OBJECT_ROUTINE: StatementKind.DROP_FUNCTION,
    enums.ObjectType.OBJECT_PROCEDURE: StatementKind.DROP_PROCEDURE,
    enums.ObjectType.OBJECT_AGGREGATE: StatementKind.DROP_AGGREGATE,
    enums.ObjectType.OBJECT_SCHEMA: StatementKind.DROP_SCHEMA,
    enums.ObjectType.OBJECT_POLICY: StatementKind.DROP_POLICY,
}

# Object classes whose dotted names denote a single relation we can target.
_DROP_RELATION_KINDS = frozenset(
    {
        StatementKind.DROP_TABLE,
        StatementKind.DROP_INDEX,
        StatementKind.DROP_INDEX_CONCURRENTLY,
        StatementKind.DROP_VIEW,
        StatementKind.DROP_MATVIEW,
        StatementKind.DROP_SEQUENCE,
    }
)

_RENAME_KINDS: dict[enums.ObjectType, StatementKind] = {
    enums.ObjectType.OBJECT_TABLE: StatementKind.RENAME_TABLE,
    enums.ObjectType.OBJECT_COLUMN: StatementKind.RENAME_COLUMN,
    enums.ObjectType.OBJECT_INDEX: StatementKind.RENAME_INDEX,
    enums.ObjectType.OBJECT_TABCONSTRAINT: StatementKind.RENAME_CONSTRAINT,
    enums.ObjectType.OBJECT_VIEW: StatementKind.RENAME_VIEW,
    enums.ObjectType.OBJECT_MATVIEW: StatementKind.RENAME_MATVIEW,
    enums.ObjectType.OBJECT_TYPE: StatementKind.RENAME_TYPE,
}


def _object_qname(entry: object) -> QualifiedName | None:
    """Name of a non-relation object reference (RenameStmt.object etc.)."""
    if isinstance(entry, ast.TypeName):
        return _qname_from_strings(_strings(entry.names))
    if isinstance(entry, tuple | list):
        return _qname_from_strings(_strings(entry))
    if isinstance(entry, ast.String) and entry.sval:
        return QualifiedName(name=entry.sval)
    if isinstance(entry, ast.ObjectWithArgs):
        return _qname_from_strings(_strings(entry.objname))
    return None

_TRANSACTION_KINDS: dict[enums.TransactionStmtKind, StatementKind] = {
    enums.TransactionStmtKind.TRANS_STMT_BEGIN: StatementKind.BEGIN,
    enums.TransactionStmtKind.TRANS_STMT_START: StatementKind.BEGIN,
    enums.TransactionStmtKind.TRANS_STMT_COMMIT: StatementKind.COMMIT,
    enums.TransactionStmtKind.TRANS_STMT_ROLLBACK: StatementKind.ROLLBACK,
    enums.TransactionStmtKind.TRANS_STMT_SAVEPOINT: StatementKind.SAVEPOINT,
    enums.TransactionStmtKind.TRANS_STMT_RELEASE: StatementKind.RELEASE_SAVEPOINT,
    enums.TransactionStmtKind.TRANS_STMT_ROLLBACK_TO: StatementKind.ROLLBACK_TO_SAVEPOINT,
}


def _drop_object_entry(entry: object) -> tuple[str | None, QualifiedName | None]:
    """Render one DropStmt.objects entry as (dotted name, relation name)."""
    if isinstance(entry, ast.String):
        return entry.sval, _qname_from_strings([entry.sval] if entry.sval else [])
    if isinstance(entry, tuple | list):
        parts = _strings(entry)
        return ".".join(parts) if parts else None, _qname_from_strings(parts)
    if isinstance(entry, ast.TypeName):
        parts = _strings(entry.names)
        return ".".join(parts) if parts else None, _qname_from_strings(parts)
    if isinstance(entry, ast.ObjectWithArgs):
        parts = _strings(entry.objname)
        return ".".join(parts) if parts else None, None
    return None, None


def _classify_drop(node: ast.DropStmt, sql: str, span: SourceSpan) -> ParsedStatement:
    if node.removeType is enums.ObjectType.OBJECT_INDEX:
        kind = (
            StatementKind.DROP_INDEX_CONCURRENTLY
            if node.concurrent
            else StatementKind.DROP_INDEX
        )
    elif node.removeType is not None and node.removeType in _DROP_KINDS:
        kind = _DROP_KINDS[node.removeType]
    else:
        kind = StatementKind.OTHER

    names: list[str] = []
    targets: list[QualifiedName] = []
    for entry in node.objects or ():
        dotted, qname = _drop_object_entry(entry)
        if dotted is not None:
            names.append(dotted)
        if qname is not None and kind in _DROP_RELATION_KINDS:
            targets.append(qname)

    return ParsedStatement(
        kind=kind,
        sql=sql,
        span=span,
        node_tag="DropStmt",
        targets=tuple(targets),
        details=DropDetails(
            object_names=tuple(names),
            cascade=node.behavior is enums.DropBehavior.DROP_CASCADE,
            missing_ok=bool(node.missing_ok),
        ),
    )


def _collect_plpgsql_queries(tree: object) -> tuple[list[tuple[str, bool]], int]:
    """Collect (statement text, guarded) pairs from a plpgsql parse tree.

    Expressions with parseMode 0 are complete SQL statements (the body of
    execsql, FOR-over-query loops, OPEN cursors, ...). Everything else is a
    fragment. Dynamic EXECUTE statements are counted, not collected. A
    statement is ``guarded`` when it sits inside an IF whose condition (or
    any ELSIF condition in the same chain) probes the system catalogs.
    """
    entries: list[tuple[str, bool]] = []
    dynamic = 0

    def condition_text(node: object) -> str | None:
        if isinstance(node, dict):
            expr = node.get("PLpgSQL_expr")
            if isinstance(expr, dict):
                query = expr.get("query")
                if isinstance(query, str):
                    return query
        return None

    def walk_if(if_node: dict[str, object], guarded: bool) -> None:
        conditions = [condition_text(if_node.get("cond"))]
        elsif_nodes: list[dict[str, object]] = []
        elsifs = if_node.get("elsif_list")
        if isinstance(elsifs, list):
            for elsif in elsifs:
                if isinstance(elsif, dict) and isinstance(
                    elsif.get("PLpgSQL_if_elsif"), dict
                ):
                    inner = elsif["PLpgSQL_if_elsif"]
                    elsif_nodes.append(inner)
                    conditions.append(condition_text(inner.get("cond")))
        chain_guarded = guarded or any(
            cond is not None and _condition_probes_catalog(cond) for cond in conditions
        )
        walk(if_node.get("then_body"), chain_guarded)
        walk(if_node.get("else_body"), chain_guarded)
        for inner in elsif_nodes:
            walk(inner.get("stmts"), chain_guarded)

    def walk(obj: object, guarded: bool) -> None:
        nonlocal dynamic
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "PLpgSQL_stmt_if" and isinstance(value, dict):
                    walk_if(value, guarded)
                elif key == "PLpgSQL_expr" and isinstance(value, dict):
                    query = value.get("query")
                    if value.get("parseMode", 0) == 0 and isinstance(query, str):
                        entries.append((query, guarded))
                    continue
                else:
                    if key == "PLpgSQL_stmt_dynexecute":
                        dynamic += 1
                    walk(value, guarded)
        elif isinstance(obj, tuple | list):
            for item in obj:
                walk(item, guarded)

    walk(tree, False)
    return entries, dynamic


def _classify_do(node: ast.DoStmt, sql: str, span: SourceSpan) -> ParsedStatement:
    language = "plpgsql"
    for elem in node.args or ():
        if (
            isinstance(elem, ast.DefElem)
            and elem.defname == "language"
            and isinstance(elem.arg, ast.String)
            and elem.arg.sval
        ):
            language = elem.arg.sval

    statements: list[ParsedStatement] = []
    dynamic = 0
    existence_guarded = False
    fully_parsed = language == "plpgsql"
    if fully_parsed:
        try:
            blocks = parse_plpgsql(sql)
        except PglastError:
            fully_parsed = False
        else:
            entries, dynamic = _collect_plpgsql_queries(blocks)
            for query, guarded in entries:
                try:
                    raw_statements = parse_sql(query)
                except PglastError:
                    fully_parsed = False
                    continue
                for raw in raw_statements:
                    if raw.stmt is None:
                        continue
                    statements.append(classify_statement(raw.stmt, query.strip(), span))
                    if guarded:
                        existence_guarded = True

    return ParsedStatement(
        kind=StatementKind.DO_BLOCK,
        sql=sql,
        span=span,
        node_tag="DoStmt",
        details=DoBlockDetails(
            language=language,
            statements=tuple(statements),
            dynamic_sql_count=dynamic,
            fully_parsed=fully_parsed,
            existence_guarded=existence_guarded,
        ),
    )


def _simple(
    kind: StatementKind,
    node: ast.Node,
    sql: str,
    span: SourceSpan,
    *rangevars: ast.RangeVar | None,
) -> ParsedStatement:
    return ParsedStatement(
        kind=kind,
        sql=sql,
        span=span,
        node_tag=type(node).__name__,
        targets=_targets(*rangevars),
    )


def classify_statement(node: ast.Node, sql: str, span: SourceSpan) -> ParsedStatement:
    """Classify one raw parse-tree statement into the pgverdict IR."""
    # -- Transactions ------------------------------------------------------
    if isinstance(node, ast.TransactionStmt):
        kind = StatementKind.TRANSACTION_OTHER
        if node.kind is not None:
            kind = _TRANSACTION_KINDS.get(node.kind, StatementKind.TRANSACTION_OTHER)
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="TransactionStmt",
            details=TransactionDetails(
                savepoint_name=node.savepoint_name, chain=bool(node.chain)
            ),
        )

    # -- ALTER on relations ------------------------------------------------
    if isinstance(node, ast.AlterTableStmt):
        kind = StatementKind.ALTER_TABLE
        if node.objtype is not None:
            kind = _ALTER_RELATION_KINDS.get(node.objtype, StatementKind.ALTER_TABLE)
        actions = tuple(
            _alter_table_action(cmd)
            for cmd in node.cmds or ()
            if isinstance(cmd, ast.AlterTableCmd)
        )
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="AlterTableStmt",
            targets=_targets(node.relation),
            alter_actions=actions,
            only=node.relation is not None and node.relation.inh is False,
        )

    # -- Tables ------------------------------------------------------------
    if isinstance(node, ast.CreateForeignTableStmt):
        # Not a CreateStmt subclass in pglast; the table shape lives in .base.
        base = node.base if isinstance(node.base, ast.CreateStmt) else None
        return ParsedStatement(
            kind=StatementKind.CREATE_FOREIGN_TABLE,
            sql=sql,
            span=span,
            node_tag="CreateForeignTableStmt",
            targets=_targets(base.relation) if base is not None else (),
            details=CreateTableDetails(
                persistence="permanent",
                if_not_exists=base is not None and bool(base.if_not_exists),
            ),
        )

    if isinstance(node, ast.CreateStmt):
        persistence = {"p": "permanent", "u": "unlogged", "t": "temporary"}.get(
            node.relation.relpersistence or "p", "permanent"
        ) if node.relation is not None else "permanent"
        referenced: list[QualifiedName] = []
        for element in node.tableElts or ():
            constraints: tuple[object, ...]
            if isinstance(element, ast.ColumnDef):
                constraints = tuple(element.constraints or ())
            else:
                constraints = (element,)
            for con in constraints:
                if (
                    isinstance(con, ast.Constraint)
                    and con.contype is enums.ConstrType.CONSTR_FOREIGN
                ):
                    pk = _qname(con.pktable)
                    if pk is not None and pk not in referenced:
                        referenced.append(pk)
        partition_of: QualifiedName | None = None
        if node.partbound is not None:
            for parent in node.inhRelations or ():
                if isinstance(parent, ast.RangeVar):
                    partition_of = _qname(parent)
                    break
        kind = (
            StatementKind.CREATE_TABLE_PARTITION_OF
            if node.partbound is not None
            else StatementKind.CREATE_TABLE
        )
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="CreateStmt",
            targets=_targets(node.relation),
            details=CreateTableDetails(
                persistence=persistence,
                if_not_exists=bool(node.if_not_exists),
                partition_of=partition_of,
                inherits=node.partbound is None and bool(node.inhRelations),
                referenced_tables=tuple(referenced),
            ),
        )

    if isinstance(node, ast.CreateTableAsStmt):
        into = node.into
        rel = into.rel if into is not None else None
        kind = (
            StatementKind.CREATE_MATVIEW
            if node.objtype is enums.ObjectType.OBJECT_MATVIEW
            else StatementKind.CREATE_TABLE_AS
        )
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="CreateTableAsStmt",
            targets=_targets(rel),
            details=CreateTableAsDetails(no_data=bool(into.skipData) if into else False),
        )

    if isinstance(node, ast.DropStmt):
        return _classify_drop(node, sql, span)

    if isinstance(node, ast.TruncateStmt):
        relations = [rel for rel in node.relations or () if isinstance(rel, ast.RangeVar)]
        return ParsedStatement(
            kind=StatementKind.TRUNCATE,
            sql=sql,
            span=span,
            node_tag="TruncateStmt",
            targets=_targets(*relations),
            details=TruncateDetails(
                cascade=node.behavior is enums.DropBehavior.DROP_CASCADE,
                restart_identity=bool(node.restart_seqs),
            ),
        )

    if isinstance(node, ast.RenameStmt):
        kind = StatementKind.RENAME_OTHER
        if node.renameType is not None:
            kind = _RENAME_KINDS.get(node.renameType, StatementKind.RENAME_OTHER)
        targets = _targets(node.relation)
        if not targets:
            object_name = _object_qname(node.object)
            if object_name is not None:
                targets = (object_name,)
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="RenameStmt",
            targets=targets,
            details=RenameDetails(
                old_name=node.subname,
                new_name=node.newname,
                object_type=node.renameType.name if node.renameType is not None else "unknown",
            ),
        )

    if isinstance(node, ast.AlterObjectSchemaStmt):
        return _simple(StatementKind.ALTER_OBJECT_SCHEMA, node, sql, span, node.relation)
    if isinstance(node, ast.AlterOwnerStmt):
        return _simple(StatementKind.ALTER_OWNER, node, sql, span, node.relation)

    # -- Indexes -----------------------------------------------------------
    if isinstance(node, ast.IndexStmt):
        kind = (
            StatementKind.CREATE_INDEX_CONCURRENTLY
            if node.concurrent
            else StatementKind.CREATE_INDEX
        )
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="IndexStmt",
            targets=_targets(node.relation),
            details=IndexDetails(
                index_name=node.idxname,
                unique=bool(node.unique),
                method=node.accessMethod or "btree",
                partial=node.whereClause is not None,
                has_expression=any(
                    isinstance(param, ast.IndexElem) and param.expr is not None
                    for param in node.indexParams or ()
                ),
                if_not_exists=bool(node.if_not_exists),
            ),
            only=node.relation is not None and node.relation.inh is False,
        )

    if isinstance(node, ast.CreateStatsStmt):
        relations = [rel for rel in node.relations or () if isinstance(rel, ast.RangeVar)]
        return _simple(StatementKind.CREATE_STATISTICS, node, sql, span, *relations)

    if isinstance(node, ast.AlterStatsStmt):
        name = _qname_from_strings(_strings(node.defnames))
        return ParsedStatement(
            kind=StatementKind.ALTER_STATISTICS,
            sql=sql,
            span=span,
            node_tag="AlterStatsStmt",
            targets=(name,) if name is not None else (),
        )

    if isinstance(node, ast.ReindexStmt):
        concurrent = any(
            isinstance(param, ast.DefElem)
            and param.defname == "concurrently"
            and _defelem_enabled(param)
            for param in node.params or ()
        )
        kind = StatementKind.REINDEX_CONCURRENTLY if concurrent else StatementKind.REINDEX
        scope = "index"
        if node.kind is not None:
            scope = node.kind.name.removeprefix("REINDEX_OBJECT_").lower()
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="ReindexStmt",
            targets=_targets(node.relation),
            details=ReindexDetails(scope=scope, object_name=node.name),
        )

    # -- Maintenance -------------------------------------------------------
    if isinstance(node, ast.VacuumStmt):
        full = any(
            isinstance(opt, ast.DefElem) and opt.defname == "full" and _defelem_enabled(opt)
            for opt in node.options or ()
        )
        if not node.is_vacuumcmd:
            kind = StatementKind.ANALYZE
        elif full:
            kind = StatementKind.VACUUM_FULL
        else:
            kind = StatementKind.VACUUM
        relations = [
            rel.relation
            for rel in node.rels or ()
            if isinstance(rel, ast.VacuumRelation) and rel.relation is not None
        ]
        return _simple(kind, node, sql, span, *relations)

    if isinstance(node, ast.ClusterStmt):
        return _simple(StatementKind.CLUSTER, node, sql, span, node.relation)

    if isinstance(node, ast.RefreshMatViewStmt):
        kind = (
            StatementKind.REFRESH_MATVIEW_CONCURRENTLY
            if node.concurrent
            else StatementKind.REFRESH_MATVIEW
        )
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="RefreshMatViewStmt",
            targets=_targets(node.relation),
            details=CreateTableAsDetails(no_data=bool(node.skipData)),
        )

    # -- Views and sequences ----------------------------------------------
    if isinstance(node, ast.ViewStmt):
        return _simple(StatementKind.CREATE_VIEW, node, sql, span, node.view)
    if isinstance(node, ast.CreateSeqStmt):
        return _simple(StatementKind.CREATE_SEQUENCE, node, sql, span, node.sequence)
    if isinstance(node, ast.AlterSeqStmt):
        return _simple(StatementKind.ALTER_SEQUENCE, node, sql, span, node.sequence)

    # -- Types and domains -------------------------------------------------
    if isinstance(node, ast.CreateEnumStmt):
        name = _qname_from_strings(_strings(node.typeName))
        return ParsedStatement(
            kind=StatementKind.CREATE_ENUM_TYPE,
            sql=sql,
            span=span,
            node_tag="CreateEnumStmt",
            targets=(name,) if name is not None else (),
        )
    if isinstance(node, ast.CompositeTypeStmt):
        return _simple(StatementKind.CREATE_COMPOSITE_TYPE, node, sql, span, node.typevar)
    if isinstance(node, ast.CreateRangeStmt):
        name = _qname_from_strings(_strings(node.typeName))
        return ParsedStatement(
            kind=StatementKind.CREATE_RANGE_TYPE,
            sql=sql,
            span=span,
            node_tag="CreateRangeStmt",
            targets=(name,) if name is not None else (),
        )
    if isinstance(node, ast.DefineStmt):
        if node.kind is enums.ObjectType.OBJECT_AGGREGATE:
            name = _qname_from_strings(_strings(node.defnames))
            return ParsedStatement(
                kind=StatementKind.CREATE_AGGREGATE,
                sql=sql,
                span=span,
                node_tag="DefineStmt",
                targets=(name,) if name is not None else (),
            )
        # CREATE OPERATOR / COLLATION / base TYPE and friends stay generic.
        return _simple(StatementKind.OTHER, node, sql, span)

    if isinstance(node, ast.AlterEnumStmt):
        kind = (
            StatementKind.ALTER_ENUM_RENAME_VALUE
            if node.oldVal is not None
            else StatementKind.ALTER_ENUM_ADD_VALUE
        )
        name = _qname_from_strings(_strings(node.typeName))
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="AlterEnumStmt",
            targets=(name,) if name is not None else (),
        )
    if isinstance(node, ast.CreateDomainStmt):
        name = _qname_from_strings(_strings(node.domainname))
        return ParsedStatement(
            kind=StatementKind.CREATE_DOMAIN,
            sql=sql,
            span=span,
            node_tag="CreateDomainStmt",
            targets=(name,) if name is not None else (),
        )
    if isinstance(node, ast.AlterDomainStmt):
        name = _qname_from_strings(_strings(node.typeName))
        return ParsedStatement(
            kind=StatementKind.ALTER_DOMAIN,
            sql=sql,
            span=span,
            node_tag="AlterDomainStmt",
            targets=(name,) if name is not None else (),
        )

    # -- Functions, triggers, policies, rules ------------------------------
    if isinstance(node, ast.CreateFunctionStmt):
        kind = (
            StatementKind.CREATE_PROCEDURE
            if node.is_procedure
            else StatementKind.CREATE_FUNCTION
        )
        name = _qname_from_strings(_strings(node.funcname))
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="CreateFunctionStmt",
            targets=(name,) if name is not None else (),
        )
    if isinstance(node, ast.AlterFunctionStmt):
        return _simple(StatementKind.ALTER_FUNCTION, node, sql, span)
    if isinstance(node, ast.CreateTrigStmt):
        return _simple(StatementKind.CREATE_TRIGGER, node, sql, span, node.relation)
    if isinstance(node, ast.CreatePolicyStmt):
        return _simple(StatementKind.CREATE_POLICY, node, sql, span, node.table)
    if isinstance(node, ast.AlterPolicyStmt):
        return _simple(StatementKind.ALTER_POLICY, node, sql, span, node.table)
    if isinstance(node, ast.RuleStmt):
        return _simple(StatementKind.CREATE_RULE, node, sql, span, node.relation)

    # -- Extensions, schemas, roles, ACLs ----------------------------------
    if isinstance(node, ast.CreateExtensionStmt):
        return _simple(StatementKind.CREATE_EXTENSION, node, sql, span)
    if isinstance(node, ast.AlterExtensionStmt):
        return _simple(StatementKind.ALTER_EXTENSION, node, sql, span)
    if isinstance(node, ast.CreateSchemaStmt):
        return _simple(StatementKind.CREATE_SCHEMA, node, sql, span)
    if isinstance(node, ast.CreateRoleStmt):
        return _simple(StatementKind.CREATE_ROLE, node, sql, span)
    if isinstance(node, ast.AlterRoleStmt | ast.AlterRoleSetStmt):
        return _simple(StatementKind.ALTER_ROLE, node, sql, span)
    if isinstance(node, ast.DropRoleStmt):
        return _simple(StatementKind.DROP_ROLE, node, sql, span)
    if isinstance(node, ast.GrantStmt | ast.GrantRoleStmt):
        kind = StatementKind.GRANT if node.is_grant else StatementKind.REVOKE
        return _simple(kind, node, sql, span)
    if isinstance(node, ast.AlterDefaultPrivilegesStmt):
        return _simple(StatementKind.ALTER_DEFAULT_PRIVILEGES, node, sql, span)
    if isinstance(node, ast.CommentStmt):
        return _simple(StatementKind.COMMENT_ON, node, sql, span)

    # -- DML ---------------------------------------------------------------
    if isinstance(node, ast.InsertStmt):
        select = node.selectStmt
        if select is None:
            source = "default_values"
        elif isinstance(select, ast.SelectStmt) and select.valuesLists is not None:
            source = "values"
        else:
            source = "select"
        return ParsedStatement(
            kind=StatementKind.INSERT,
            sql=sql,
            span=span,
            node_tag="InsertStmt",
            targets=_targets(node.relation),
            details=InsertDetails(source=source),
        )
    if isinstance(node, ast.UpdateStmt | ast.DeleteStmt):
        return _classify_dml(node, sql, span)
    if isinstance(node, ast.MergeStmt):
        return _simple(StatementKind.MERGE, node, sql, span, node.relation)
    if isinstance(node, ast.SelectStmt):
        if node.intoClause is not None:
            return _simple(StatementKind.SELECT_INTO, node, sql, span, node.intoClause.rel)
        return _simple(StatementKind.SELECT, node, sql, span)
    if isinstance(node, ast.CopyStmt):
        kind = StatementKind.COPY_FROM if node.is_from else StatementKind.COPY_TO
        return _simple(kind, node, sql, span, node.relation)
    if isinstance(node, ast.CallStmt):
        return _simple(StatementKind.CALL, node, sql, span)

    # -- Session / server state --------------------------------------------
    if isinstance(node, ast.VariableSetStmt):
        kind = (
            StatementKind.RESET
            if node.kind
            in (enums.VariableSetKind.VAR_RESET, enums.VariableSetKind.VAR_RESET_ALL)
            else StatementKind.SET
        )
        return ParsedStatement(
            kind=kind,
            sql=sql,
            span=span,
            node_tag="VariableSetStmt",
            details=SetDetails(name=node.name, is_local=bool(node.is_local)),
        )
    if isinstance(node, ast.VariableShowStmt):
        return _simple(StatementKind.SHOW, node, sql, span)
    if isinstance(node, ast.LockStmt):
        relations = [rel for rel in node.relations or () if isinstance(rel, ast.RangeVar)]
        mode = node.mode if node.mode is not None else 8
        return ParsedStatement(
            kind=StatementKind.LOCK_TABLE,
            sql=sql,
            span=span,
            node_tag="LockStmt",
            targets=_targets(*relations),
            details=LockDetails(
                mode=mode,
                mode_name=_LOCK_MODE_NAMES.get(mode, "UNKNOWN"),
                nowait=bool(node.nowait),
            ),
        )
    if isinstance(node, ast.AlterSystemStmt):
        return _simple(StatementKind.ALTER_SYSTEM, node, sql, span)
    if isinstance(node, ast.CreatedbStmt):
        return _simple(StatementKind.CREATE_DATABASE, node, sql, span)
    if isinstance(node, ast.AlterDatabaseStmt | ast.AlterDatabaseSetStmt):
        return _simple(StatementKind.ALTER_DATABASE, node, sql, span)
    if isinstance(node, ast.DropdbStmt):
        return _simple(StatementKind.DROP_DATABASE, node, sql, span)
    if isinstance(node, ast.CreateTableSpaceStmt):
        return _simple(StatementKind.CREATE_TABLESPACE, node, sql, span)
    if isinstance(node, ast.DropTableSpaceStmt):
        return _simple(StatementKind.DROP_TABLESPACE, node, sql, span)

    if isinstance(node, ast.DoStmt):
        return _classify_do(node, sql, span)

    return _simple(StatementKind.OTHER, node, sql, span)
