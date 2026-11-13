"""Reference implementation of minisql for long_task.py. The agent never sees this file.

It exists to prove that every checkpoint can be passed honestly.
"""
import re

KEYWORDS = set("""select from where group by having order limit offset as and or not is null in like between
case when then else end distinct join inner left outer cross on asc desc union all intersect except exists
with recursive over partition rows unbounded preceding following current row""".split())
AGGS = {"count", "sum", "min", "max"}
TOKEN = re.compile(r"\s*(?:(\d+)|'((?:[^']|'')*)'|([A-Za-z_][A-Za-z_0-9]*)|(<=|>=|<>|!=|\|\||[-+*/%(),.=<>]))")
INF = float("inf")


class SqlError(Exception):
    pass


def tokenize(sql):
    sql = sql.strip().rstrip(";").rstrip()
    out, pos = [], 0
    while pos < len(sql):
        m = TOKEN.match(sql, pos)
        if not m:
            raise SqlError(f"cannot read the query at: {sql[pos:pos + 20]!r}")
        pos = m.end()
        if m.group(1) is not None:
            out.append(("num", int(m.group(1))))
        elif m.group(2) is not None:
            out.append(("str", m.group(2).replace("''", "'")))
        elif m.group(3) is not None:
            word = m.group(3).lower()
            out.append(("kw" if word in KEYWORDS else "id", word))
        else:
            out.append(("op", m.group(4)))
    return out + [("end", None)] * 4


class Parser:
    def __init__(self, sql):
        self.t, self.i = tokenize(sql), 0

    def peek(self, k=0):
        return self.t[self.i + k]

    def kw(self, *words):
        if all(self.t[self.i + k] == ("kw", w) for k, w in enumerate(words)):
            self.i += len(words)
            return True
        return False

    def op(self, s):
        if self.t[self.i] == ("op", s):
            self.i += 1
            return True
        return False

    def need(self, kind, s):
        if not (self.kw(s) if kind == "kw" else self.op(s)):
            raise SqlError(f"expected {s!r}, found {self.peek()[1]!r}")

    def ident(self):
        tok = self.peek()
        if tok[0] != "id":
            raise SqlError(f"expected a name, found {tok[1]!r}")
        self.i += 1
        return tok[1]

    def many(self, item):
        out = [item()]
        while self.op(","):
            out.append(item())
        return out

    def order_term(self):
        e = self.expr()
        desc = self.kw("desc")
        if not desc:
            self.kw("asc")
        return (e, desc)

    def starts_query(self):
        return self.peek() in (("kw", "select"), ("kw", "with"))

    def query(self):
        ctes, recursive = [], False
        if self.kw("with"):
            recursive = self.kw("recursive")
            while True:
                name, cols = self.ident(), None
                if self.op("("):
                    cols = self.many(self.ident)
                    self.need("op", ")")
                self.need("kw", "as")
                self.need("op", "(")
                ctes.append((name, cols, self.query()))
                self.need("op", ")")
                if not self.op(","):
                    break
        parts, ops = [self.select()], []
        while True:
            if self.kw("union"):
                ops.append("union all" if self.kw("all") else "union")
            elif self.kw("intersect"):
                ops.append("intersect")
            elif self.kw("except"):
                ops.append("except")
            else:
                break
            parts.append(self.select())
        order = self.many(self.order_term) if self.kw("order", "by") else []
        limit = offset = None
        if self.kw("limit"):
            limit = self.expr()
            if self.kw("offset"):
                offset = self.expr()
        return {"ctes": ctes, "recursive": recursive, "parts": parts, "ops": ops, "order": order,
                "limit": limit, "offset": offset}

    def item(self):
        if self.op("*"):
            return ("star", None)
        if self.peek()[0] == "id" and self.peek(1) == ("op", ".") and self.peek(2) == ("op", "*"):
            name = self.ident()
            self.i += 2
            return ("star", name)
        e, alias = self.expr(), None
        if self.kw("as") or self.peek()[0] == "id":
            alias = self.ident()
        return ("expr", e, alias)

    def select(self):
        self.need("kw", "select")
        s = {"distinct": self.kw("distinct"), "from": [], "where": None, "group": [], "having": None}
        s["items"] = self.many(self.item)
        if self.kw("from"):
            s["from"].append(("inner", self.source(), None))
            while True:
                if self.op(",") or self.kw("cross", "join") or self.kw("inner", "join") or self.kw("join"):
                    kind = "inner"
                elif self.kw("left", "outer", "join") or self.kw("left", "join"):
                    kind = "left"
                else:
                    break
                src = self.source()
                s["from"].append((kind, src, self.expr() if self.kw("on") else None))
        if self.kw("where"):
            s["where"] = self.expr()
        if self.kw("group", "by"):
            s["group"] = self.many(self.expr)
        if self.kw("having"):
            s["having"] = self.expr()
        return s

    def source(self):
        if self.op("("):
            q = self.query()
            self.need("op", ")")
            self.kw("as")
            return ("sub", q, self.ident())
        name = alias = self.ident()
        if self.kw("as") or self.peek()[0] == "id":
            alias = self.ident()
        return ("table", name, alias)

    def expr(self):
        e = self.and_()
        while self.kw("or"):
            e = ("or", e, self.and_())
        return e

    def and_(self):
        e = self.not_()
        while self.kw("and"):
            e = ("and", e, self.not_())
        return e

    def not_(self):
        return ("not", self.not_()) if self.kw("not") else self.cmp()

    def cmp(self):
        e = self.add()
        while True:
            tok = self.peek()
            if tok[0] == "op" and tok[1] in ("=", "!=", "<>", "<", "<=", ">", ">="):
                self.i += 1
                e = ("cmp", "!=" if tok[1] == "<>" else tok[1], e, self.add())
            elif self.kw("is", "not", "null"):
                e = ("isnull", e, False)
            elif self.kw("is", "null"):
                e = ("isnull", e, True)
            else:
                save = self.i
                neg = self.kw("not")
                if self.kw("between"):
                    lo = self.add()
                    self.need("kw", "and")
                    r = ("and", ("cmp", ">=", e, lo), ("cmp", "<=", e, self.add()))
                elif self.kw("like"):
                    r = ("like", e, self.add())
                elif self.kw("in"):
                    self.need("op", "(")
                    r = ("insub", e, self.query()) if self.starts_query() else ("inlist", e, self.many(self.expr))
                    self.need("op", ")")
                else:
                    self.i = save
                    return e
                e = ("not", r) if neg else r

    def add(self):
        e = self.mul()
        while self.peek() in (("op", "+"), ("op", "-")):
            self.i += 1
            e = ("arith", self.t[self.i - 1][1], e, self.mul())
        return e

    def mul(self):
        e = self.concat()
        while self.peek() in (("op", "*"), ("op", "/"), ("op", "%")):
            self.i += 1
            e = ("arith", self.t[self.i - 1][1], e, self.concat())
        return e

    def concat(self):
        e = self.unary()
        while self.op("||"):
            e = ("concat", e, self.unary())
        return e

    def unary(self):
        return ("neg", self.unary()) if self.op("-") else self.primary()

    def primary(self):
        tok = self.peek()
        if tok[0] in ("num", "str"):
            self.i += 1
            return ("lit", tok[1])
        if self.kw("null"):
            return ("lit", None)
        if self.kw("exists"):
            self.need("op", "(")
            q = self.query()
            self.need("op", ")")
            return ("exists", q)
        if self.kw("case"):
            base = None if self.peek() == ("kw", "when") else self.expr()
            whens = []
            while self.kw("when"):
                c = self.expr()
                self.need("kw", "then")
                whens.append((c, self.expr()))
            other = self.expr() if self.kw("else") else ("lit", None)
            self.need("kw", "end")
            return ("case", base, whens, other)
        if self.op("("):
            e = ("subq", self.query()) if self.starts_query() else self.expr()
            self.need("op", ")")
            return e
        if tok[0] == "id":
            name = self.ident()
            if self.op("("):
                return self.call(name)
            if self.op("."):
                return ("col", name, self.ident())
            return ("col", None, name)
        raise SqlError(f"unexpected {tok[1]!r}")

    def call(self, name):
        distinct, args = False, []
        if self.op("*"):
            args = None
        elif self.peek() != ("op", ")"):
            distinct = self.kw("distinct")
            args = self.many(self.expr)
        self.need("op", ")")
        if self.kw("over"):
            self.need("op", "(")
            part = self.many(self.expr) if self.kw("partition", "by") else []
            order = self.many(self.order_term) if self.kw("order", "by") else []
            frame = None
            if self.kw("rows", "between"):
                lo = self.bound()
                self.need("kw", "and")
                frame = (lo, self.bound())
            self.need("op", ")")
            return ("win", name, args, part, order, frame)
        if name in AGGS:
            return ("agg", name, args, distinct)
        return ("fn", name, args)

    def bound(self):
        if self.kw("unbounded", "preceding"):
            return -INF
        if self.kw("unbounded", "following"):
            return INF
        if self.kw("current", "row"):
            return 0
        n = self.peek()[1]
        self.i += 1
        if self.kw("preceding"):
            return -n
        self.need("kw", "following")
        return n


class Env:
    def __init__(self, cols, vals, parent=None):
        self.cols, self.vals, self.parent, self.group, self.win = cols, vals, parent, None, {}

    def get(self, table, name):
        e = self
        while e is not None:
            for i, (t, n) in enumerate(e.cols):
                if n == name and (table is None or t == table):
                    return e.vals[i]
            e = e.parent
        raise SqlError(f"no such column: {name}")


def truth(v):
    return v is not None and v != 0


def sort_key(v):
    return (0, 0) if v is None else (2, v) if isinstance(v, str) else (1, v)


def dedupe(rows):
    return list(dict.fromkeys(rows))


def in3(x, vals):
    if not vals:
        return 0
    if x is None:
        return None
    if x in [v for v in vals if v is not None]:
        return 1
    return None if None in vals else 0


def find(n, tag):
    if isinstance(n, tuple) and n and n[0] == tag:
        yield n
    elif isinstance(n, tuple) and n and n[0] == "win":
        return
    elif isinstance(n, (tuple, list)):
        for c in n:
            yield from find(c, tag)


def aggregate(name, args, distinct, rows, ctx):
    if args is None:
        return len(rows)
    vals = [v for v in (ev(args[0], r, ctx) for r in rows) if v is not None]
    if distinct:
        vals = dedupe(vals)
    if name == "count":
        return len(vals)
    if not vals:
        return None
    return sum(vals) if name == "sum" else min(vals) if name == "min" else max(vals)


def call_fn(name, a):
    if name == "coalesce":
        return next((v for v in a if v is not None), None)
    if name == "nullif":
        return None if a[0] == a[1] else a[0]
    if a[0] is None:
        return None
    if name == "upper":
        return str(a[0]).upper()
    if name == "lower":
        return str(a[0]).lower()
    if name == "length":
        return len(str(a[0]))
    if name == "abs":
        return abs(a[0])
    if name == "substr":
        start = a[1] - 1
        return str(a[0])[start:] if len(a) < 3 else str(a[0])[start:start + a[2]]
    raise SqlError(f"no such function: {name}")


def ev(n, env, ctx):
    k = n[0]
    if k == "lit":
        return n[1]
    if k == "col":
        return env.get(n[1], n[2])
    if k == "neg":
        v = ev(n[1], env, ctx)
        return None if v is None else -v
    if k in ("arith", "cmp"):
        a, b = ev(n[2], env, ctx), ev(n[3], env, ctx)
        if a is None or b is None:
            return None
        op = n[1]
        if k == "cmp":
            return int({"=": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op])
        if op in "/%":
            if b == 0:
                return None
            q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
            return q if op == "/" else a - b * q
        return a + b if op == "+" else a - b if op == "-" else a * b
    if k == "concat":
        a, b = ev(n[1], env, ctx), ev(n[2], env, ctx)
        return None if a is None or b is None else str(a) + str(b)
    if k in ("and", "or"):
        a, b = ev(n[1], env, ctx), ev(n[2], env, ctx)
        if k == "and":
            return 0 if a == 0 or b == 0 else None if a is None or b is None else 1
        return 1 if truth(a) or truth(b) else None if a is None or b is None else 0
    if k == "not":
        v = ev(n[1], env, ctx)
        return None if v is None else int(not truth(v))
    if k == "isnull":
        return int((ev(n[1], env, ctx) is None) == n[2])
    if k == "like":
        a, p = ev(n[1], env, ctx), ev(n[2], env, ctx)
        if a is None or p is None:
            return None
        rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in str(p))
        return int(re.fullmatch(rx, str(a), re.IGNORECASE | re.DOTALL) is not None)
    if k == "inlist":
        return in3(ev(n[1], env, ctx), [ev(i, env, ctx) for i in n[2]])
    if k == "insub":
        return in3(ev(n[1], env, ctx), [r[0] for r in run(n[2], ctx, env)[1]])
    if k == "subq":
        rows = run(n[1], ctx, env)[1]
        return rows[0][0] if rows else None
    if k == "exists":
        return int(bool(run(n[1], ctx, env)[1]))
    if k == "case":
        for cond, result in n[2]:
            c = ev(cond, env, ctx)
            if truth(c) if n[1] is None else (c is not None and c == ev(n[1], env, ctx)):
                return ev(result, env, ctx)
        return ev(n[3], env, ctx)
    if k == "fn":
        return call_fn(n[1], [ev(a, env, ctx) for a in n[2]])
    if k == "agg":
        if env.group is None:
            raise SqlError("aggregate used outside an aggregate query")
        return aggregate(n[1], n[2], n[3], env.group, ctx)
    if k == "win":
        return env.win[id(n)]
    raise SqlError(f"cannot evaluate {k}")


def window(w, envs, ctx):
    _, name, args, part, order, frame = w
    parts = {}
    for e in envs:
        parts.setdefault(tuple(ev(p, e, ctx) for p in part), []).append(e)
    for rows in parts.values():
        for x, desc in reversed(order):
            rows.sort(key=lambda e: sort_key(ev(x, e, ctx)), reverse=desc)
        keys = [tuple(ev(x, e, ctx) for x, _ in order) for e in rows]
        n = len(rows)
        for i, e in enumerate(rows):
            first = keys.index(keys[i])
            if name == "row_number":
                v = i + 1
            elif name == "rank":
                v = first + 1
            elif name == "dense_rank":
                v = len(set(keys[:first])) + 1
            elif name == "ntile":
                size, rem = divmod(n, ev(args[0], e, ctx))
                v = i // (size + 1) + 1 if i < rem * (size + 1) else rem + (i - rem * (size + 1)) // size + 1
            elif name in ("lag", "lead"):
                off = ev(args[1], e, ctx) if len(args) > 1 else 1
                j = i - off if name == "lag" else i + off
                v = ev(args[0], rows[j], ctx) if 0 <= j < n else ev(args[2], e, ctx) if len(args) > 2 else None
            else:
                if frame:
                    lo, hi = max(0, i + frame[0]), min(n - 1, i + frame[1])
                elif order:
                    lo, hi = 0, max(j for j in range(n) if keys[j] == keys[i])
                else:
                    lo, hi = 0, n - 1
                span = rows[int(lo):int(hi) + 1]
                if name in ("first_value", "last_value"):
                    v = ev(args[0], span[0 if name == "first_value" else -1], ctx) if span else None
                elif name in AGGS:
                    v = aggregate(name, args, False, span, ctx)
                else:
                    raise SqlError(f"no such window function: {name}")
            e.win[id(w)] = v


def order_rows(pairs, order, names, ctx):
    def value(pair, x):
        if x[0] == "lit" and isinstance(x[1], int):
            return pair[0][x[1] - 1]
        if x[0] == "col" and x[1] is None and x[2] in names:
            return pair[0][names.index(x[2])]
        return ev(x, pair[1], ctx)
    for x, desc in reversed(order):
        pairs.sort(key=lambda p: sort_key(value(p, x)), reverse=desc)
    return pairs


def cut(rows, limit, offset, ctx):
    empty = Env([], ())
    start = ev(offset, empty, ctx) if offset is not None else 0
    return rows[start:] if limit is None else rows[start:start + ev(limit, empty, ctx)]


def select(s, ctx, outer, order=(), limit=None, offset=None):
    cols, rows = [], [()]
    for kind, src, on in s["from"]:
        if src[0] == "table":
            if src[1] not in ctx:
                raise SqlError(f"no such table: {src[1]}")
            names, data = ctx[src[1]]
        else:
            names, data = run(src[1], ctx, outer)
        both, new = cols + [(src[2], n) for n in names], []
        for left in rows:
            hit = False
            for right in data:
                vals = left + tuple(right)
                if on is None or truth(ev(on, Env(both, vals, outer), ctx)):
                    new.append(vals)
                    hit = True
            if kind == "left" and not hit:
                new.append(left + (None,) * len(names))
        cols, rows = both, new
    envs = [Env(cols, r, outer) for r in rows]
    if s["where"] is not None:
        envs = [e for e in envs if truth(ev(s["where"], e, ctx))]
    items = []
    for it in s["items"]:
        if it[0] == "star":
            items += [(("col", t, n), n) for t, n in cols if it[1] is None or t == it[1]]
        else:
            items.append((it[1], it[2] or (it[1][2] if it[1][0] == "col" else f"column{len(items) + 1}")))
    exprs = [e for e, _ in items] + [e for e, _ in order] + ([s["having"]] if s["having"] is not None else [])
    if s["group"] or any(True for e in exprs for _ in find(e, "agg")) or \
            any(True for e in exprs for w in find(e, "win") for _ in find(list(w[2] or []) + w[4], "agg")):
        groups = {}
        for e in envs:
            groups.setdefault(tuple(ev(g, e, ctx) for g in s["group"]), []).append(e)
        if not s["group"] and not groups:
            groups[()] = []
        envs = []
        for members in groups.values():
            g = Env(cols, members[0].vals if members else (None,) * len(cols), outer)
            g.group = members
            if s["having"] is None or truth(ev(s["having"], g, ctx)):
                envs.append(g)
    for e in exprs:
        for w in find(e, "win"):
            window(w, envs, ctx)
    names = [n for _, n in items]
    pairs = [(tuple(ev(e, env, ctx) for e, _ in items), env) for env in envs]
    if s["distinct"]:
        pairs = list({p[0]: p for p in reversed(pairs)}.values())[::-1]
    if order:
        pairs = order_rows(pairs, order, names, ctx)
    return names, cut([p[0] for p in pairs], limit, offset, ctx)


def run(q, ctx, outer=None):
    ctx = dict(ctx)
    for name, cols, body in q["ctes"]:
        again = q["recursive"] and len(body["parts"]) == 2 and body["ops"][0].startswith("union") and \
            any(src[:2] == ("table", name) for _, src, _ in body["parts"][1]["from"])
        if not again:
            names, rows = run(body, ctx, outer)
            ctx[name] = (cols or names, rows)
            continue
        plain = {"ctes": [], "recursive": False, "ops": [], "order": [], "limit": None, "offset": None}
        names, rows = run(dict(plain, parts=body["parts"][:1]), ctx, outer)
        names, distinct = cols or names, body["ops"][0] == "union"
        if distinct:
            rows = dedupe(rows)
        seen, total, work = set(rows), list(rows), rows
        while work:
            new = run(dict(plain, parts=body["parts"][1:]), dict(ctx, **{name: (names, work)}), outer)[1]
            if distinct:
                new = [r for r in dedupe(new) if r not in seen]
                seen.update(new)
            total += new
            work = new
            if len(total) > 100000:
                raise SqlError("recursive query does not end")
        ctx[name] = (names, cut(order_rows([(r, Env([(None, n) for n in names], r)) for r in total],
                                           body["order"], names, ctx), body["limit"], body["offset"], ctx)
                     if body["order"] or body["limit"] is not None else total)
    if len(q["parts"]) == 1:
        return select(q["parts"][0], ctx, outer, q["order"], q["limit"], q["offset"])
    names, rows = select(q["parts"][0], ctx, outer)
    for op, part in zip(q["ops"], q["parts"][1:]):
        more = select(part, ctx, outer)[1]
        if op == "union all":
            rows = rows + more
        elif op == "union":
            rows = dedupe(rows + more)
        else:
            rows = [r for r in dedupe(rows) if (r in set(more)) == (op == "intersect")]
    pairs = [(r, Env([(None, n) for n in names], r, outer)) for r in rows]
    return names, cut([p[0] for p in order_rows(pairs, q["order"], names, ctx)], q["limit"], q["offset"], ctx)


class Database:
    def __init__(self, tables):
        self.tables = {name.lower(): ([c.lower() for c in cols], [tuple(r) for r in rows])
                       for name, (cols, rows) in tables.items()}

    def query(self, sql):
        p = Parser(sql)
        q = p.query()
        if p.peek()[0] != "end":
            raise SqlError(f"unexpected {p.peek()[1]!r}")
        return run(q, self.tables)[1]
