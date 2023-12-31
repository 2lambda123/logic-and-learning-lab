import time
from pysat.formula import CNF
from pysat.solvers import Solver
from pysat.card import *
import clingo
import operator
import numbers
import clingo.script
import pkg_resources
from . core import Literal, RuleVar, VarVar, Var
from collections import defaultdict
from . util import rule_is_recursive, format_rule, Constraint, format_prog, order_rule, order_prog, bias_order
clingo.script.enable_python()
from clingo import Function, Number, Tuple_
from itertools import permutations

arg_lookup = {clingo.Number(i):chr(ord('A') + i) for i in range(100)}

def arg_to_symbol(arg):
    if isinstance(arg, tuple):
        return Tuple_(tuple(arg_to_symbol(a) for a in arg))
    if isinstance(arg, numbers.Number):
        return Number(arg)
    if isinstance(arg, str):
        return Function(arg)
    assert False, f'Unhandled argtype({type(arg)}) in aspsolver.py arg_to_symbol()'

def atom_to_symbol(pred, args):
    xs = tuple(arg_to_symbol(arg) for arg in args)
    return Function(name = pred, arguments = xs)

def find_all_vars(body):
    all_vars = set()
    for literal in body:
        for arg in literal.arguments:
            if isinstance(arg, Var):
                all_vars.add(arg)
            elif isinstance(arg, tuple):
                for t_arg in arg:
                    if isinstance(t_arg, Var):
                        all_vars.add(t_arg)
    return all_vars

def find_all_vars2(body):
    # head, body = rule
    all_vars = set()
    for literal in body:
        for x in literal.arguments:
            all_vars.add(x)
    return all_vars

# AC: When grounding constraint rules, we only care about the vars and the constraints, not the actual literals
def grounding_hash(body, all_vars):
    cons = set()
    for lit in body:
        if lit.meta:
            cons.add((lit.predicate, lit.arguments))
    return hash((frozenset(all_vars), frozenset(cons)))

cached_grounded = {}
def ground_literal(literal, assignment, tmp):
    k = hash((literal.predicate, literal.arguments, tmp))
    if k in cached_grounded:
        v = cached_grounded[k]
        return literal.positive, literal.predicate, v
    ground_args = []
    for arg in literal.arguments:
        if isinstance(arg, tuple):
            ground_args.append(tuple(assignment[t_arg] for t_arg in arg))
        elif arg in assignment:
            ground_args.append(assignment[arg])
        else:
            ground_args.append(arg)
    ground_args = tuple(ground_args)
    cached_grounded[k] = ground_args
    return literal.positive, literal.predicate, ground_args

def ground_rule(rule, assignment):
    k = hash(frozenset(assignment.items()))
    head, body = rule
    ground_head = None
    if head:
        ground_head = ground_literal(head, assignment, k)
    ground_body = frozenset(ground_literal(literal, assignment, k) for literal in body)
    return ground_head, ground_body

def make_literal_handle(literal):
    return f'{literal.predicate}{"".join(literal.arguments)}'

cached_handles = {}
def make_rule_handle(rule):
    k = hash(rule)
    if k in cached_handles:
        return cached_handles[k]
    head, body = rule
    body_literals = sorted(body, key = operator.attrgetter('predicate'))
    handle = ''.join(make_literal_handle(literal) for literal in [head] + body_literals)
    cached_handles[k] = handle
    return handle

def build_seen_rule(rule, is_rec):
    rule_var = vo_clause('l')
    handle = make_rule_handle(rule)
    head = Literal('seen_rule', (handle, rule_var))
    body = []
    body.extend(build_rule_literals(rule, rule_var))
    if is_rec:
        body.append(gteq(rule_var, 1))
    return head, tuple(body)

def build_seen_rule_literal(handle, rule_var):
    return Literal('seen_rule', (handle, rule_var))

def parse_model_recursion(settings, model):
    rule_index_to_body = defaultdict(set)
    rule_index_to_head = {}
    rule_index_ordering = defaultdict(set)

    head = settings.head_literal
    cached_literals = settings.cached_literals

    for atom in model:
        name = atom.name
        if name == 'body_literal':
            args = atom.arguments
            rule_index = args[0].number
            predicate = args[1].name
            atom_args = tuple(args[3].arguments)
            literal = cached_literals[(predicate, atom_args)]
            rule_index_to_body[rule_index].add(literal)
        elif name == 'head_literal':
            args = atom.arguments
            rule_index = args[0].number
            rule_index_to_head[rule_index] = head

    prog = []
    rule_lookup = {}

    directions = settings.directions

    for rule_index in rule_index_to_head:
        body = frozenset(rule_index_to_body[rule_index])
        rule = head, body
        prog.append((rule))
        rule_lookup[rule_index] = rule

    rule_ordering = defaultdict(set)
    for r1_index, lower_rule_indices in rule_index_ordering.items():
        r1 = rule_lookup[r1_index]
        rule_ordering[r1] = set(rule_lookup[r2_index] for r2_index in lower_rule_indices)

    return frozenset(prog), rule_ordering, directions

def parse_model_single_rule(settings, model):
    head = settings.head_literal
    body = set()
    directions = settings.directions
    cached_literals = settings.cached_literals
    for atom in model:
        args = atom.arguments
        predicate = args[1].name
        atom_args = tuple(args[3].arguments)
        literal = cached_literals[(predicate, atom_args)]
        body.add(literal)
    rule = head, frozenset(body)
    return frozenset([rule]), defaultdict(set), directions

def parse_model_pi(settings, model):
    directions = defaultdict(lambda: defaultdict(lambda: '?'))
    rule_index_to_body = defaultdict(set)
    rule_index_to_head = {}
    rule_index_ordering = defaultdict(set)

    for atom in model:
        args = atom.arguments
        name = atom.name

        if name == 'body_literal':
            rule_index = args[0].number
            predicate = args[1].name
            atom_args = args[3].arguments
            atom_args = settings.cached_atom_args[tuple(atom_args)]
            arity = len(atom_args)
            body_literal = (predicate, atom_args, arity)
            rule_index_to_body[rule_index].add(body_literal)

        elif name == 'head_literal':
            rule_index = args[0].number
            predicate = args[1].name
            atom_args = args[3].arguments
            atom_args = settings.cached_atom_args[tuple(atom_args)]
            arity = len(atom_args)
            head_literal = (predicate, atom_args, arity)
            rule_index_to_head[rule_index] = head_literal

        # TODO AC: STOP READING THESE THE MODELS
        elif name == 'direction_':
            pred_name = args[0].name
            arg_index = args[1].number
            arg_dir_str = args[2].name

            if arg_dir_str == 'in':
                arg_dir = '+'
            elif arg_dir_str == 'out':
                arg_dir = '-'
            else:
                raise Exception(f'Unrecognised argument direction "{arg_dir_str}"')
            directions[pred_name][arg_index] = arg_dir

        elif name == 'before':
            rule1 = args[0].number
            rule2 = args[1].number
            rule_index_ordering[rule1].add(rule2)

    prog = []
    rule_lookup = {}

    for rule_index in rule_index_to_head:
        head_pred, head_args, head_arity = rule_index_to_head[rule_index]
        head_modes = tuple(directions[head_pred][i] for i in range(head_arity))
        head = Literal(head_pred, head_args, head_modes)
        body = set()
        for (body_pred, body_args, body_arity) in rule_index_to_body[rule_index]:
            body_modes = tuple(directions[body_pred][i] for i in range(body_arity))
            body.add(Literal(body_pred, body_args, body_modes))
        body = frozenset(body)
        rule = head, body
        prog.append((rule))
        rule_lookup[rule_index] = rule

    rule_ordering = defaultdict(set)
    for r1_index, lower_rule_indices in rule_index_ordering.items():
        r1 = rule_lookup[r1_index]
        rule_ordering[r1] = set(rule_lookup[r2_index] for r2_index in lower_rule_indices)

    return frozenset(prog), rule_ordering, directions

def build_rule_literals(rule, rule_var):
    literals = []
    head, body = rule
    yield Literal('head_literal', (rule_var, head.predicate, len(head.arguments), tuple(vo_variable2(rule_var, v) for v in head.arguments)))

    for body_literal in body:
        yield Literal('body_literal', (rule_var, body_literal.predicate, len(body_literal.arguments), tuple(vo_variable2(rule_var, v) for v in body_literal.arguments)))
    for idx, var in enumerate(head.arguments):
        yield eq(vo_variable2(rule_var, var), idx)

    if rule_is_recursive(rule):
        yield gteq(rule_var, 1)

def build_rule_ordering_literals(rule_index, rule_ordering):
    for r1, higher_rules in rule_ordering.items():
        r1v = rule_index[r1]
        for r2 in higher_rules:
            r2v = rule_index[r2]
            yield lt(r1v, r2v)

class Generator:

    def __init__(self, settings, grounder, bkcons=[]):
        self.savings = 0
        self.settings = settings
        self.grounder = grounder
        self.seen_handles = set()
        self.assigned = {}
        self.seen_symbols = {}
        self.cached_clingo_atoms = {}
        self.handle = None

        # handles for rules that are minimal and unsatisfiable
        self.bad_handles = set()
        # new rules added to the solver, such as: seen(id):- head_literal(...), body_literal(...)
        self.all_handles = set()

        # TODO: dunno
        self.all_ground_cons = set()
        # TODO: dunno
        self.new_ground_cons = set()

        encoding = []
        alan = pkg_resources.resource_string(__name__, "lp/alan.pl").decode()
        encoding.append(alan)
        with open(settings.bias_file) as f:
            encoding.append(f.read())
        encoding.append(f'max_clauses({settings.max_rules}).')
        encoding.append(f'max_body({settings.max_body}).')
        encoding.append(f'max_vars({settings.max_vars}).')
        max_size = (1 + settings.max_body) * settings.max_rules
        if settings.max_literals < max_size:
            encoding.append(f'custom_max_size({settings.max_literals}).')

        if settings.pi_enabled:
            encoding.append(f'#show direction_/3.')

        if settings.pi_enabled or settings.recursion_enabled:
            encoding.append(f'#show head_literal/4.')

        if settings.noisy:
            encoding.append("""
            program_bounds(0..K):- max_size(K).
            program_size_at_least(M):- size(N), program_bounds(M), M <= N.
            """)

        if settings.bkcons:
            encoding.extend(bkcons)

        # FG Heuristic for single solve
        # - considering a default order of minimum rules, then minimum literals, and then minimum variables
        # - considering a preference for minimum hspace size parameters configuration
        if settings.single_solve:
            if settings.order_space:
                horder = bias_order(settings, max_size)
                iorder = 1
                for (size, n_vars, n_rules, _) in horder:
                    encoding.append(f'h_order({iorder},{size},{n_vars},{n_rules}).')
                    iorder += 1
                HSPACE_HEURISTIC = """
                #heuristic hspace(N). [1000-N@30,true]
                hspace(N) :- h_order(N,K,V,R), size(K), size_vars(V), size_rules(R).
                size_vars(V):- #count{K : clause_var(_,K)} == V.
                size_rules(R):- #count{K : clause(K)} == R.
                """

                encoding.append(HSPACE_HEURISTIC)
            elif settings.no_bias:
                DEFAULT_HEURISTIC = """
                size_vars(V):- #count{K : clause_var(_,K)} == V.
                size_rules(R):- #count{K : clause(K)} == R.
                #heuristic size_rules(R). [1500-R@30,true]
                #heuristic size(N). [1000-N@20,true]
                #heuristic size_vars(V). [500-V@10,true]
                """
                encoding.append(DEFAULT_HEURISTIC)
            else:
                DEFAULT_HEURISTIC = """
                #heuristic size(N). [1000-N,true]
                """
                encoding.append(DEFAULT_HEURISTIC)

        encoding = '\n'.join(encoding)

        # with open('ENCODING-GEN.pl', 'w') as f:
            # f.write(encoding)

        if self.settings.single_solve:
            solver = clingo.Control(['--heuristic=Domain','-Wnone'])
        else:
            solver = clingo.Control(['-Wnone'])
            NUM_OF_LITERALS = """
            %%% External atom for number of literals in the program %%%%%
            #external size_in_literals(n).
            :-
                size_in_literals(n),
                #sum{K+1,Clause : body_size(Clause,K)} != n.
            """
            solver.add('number_of_literals', ['n'], NUM_OF_LITERALS)

            if self.settings.no_bias:
                NUM_OF_VARS = """
                %%% External atom for number of variables in the program %%%%%
                #external size_in_vars(v).
                :-
                    size_in_vars(v),
                    #max{V : clause_var(_,V)} != v - 1.
                """
                solver.add('number_of_vars', ['v'], NUM_OF_VARS)

                NUM_OF_RULES = """
                %%% External atom for number of rules in the program %%%%%
                #external size_in_rules(r).
                :-
                    size_in_rules(r),
                    #max{R : clause(R)} != r - 1.
                """
                solver.add('number_of_rules', ['r'], NUM_OF_RULES)



        solver.configuration.solve.models = 0


        solver.add('base', [], encoding)
        solver.ground([('base', [])])
        self.solver = solver



    def get_model(self):
        if self.handle == None:
            self.handle = iter(self.solver.solve(yield_ = True))
        return next(self.handle, None)

    def gen_symbol(self, literal, backend):
        sign, pred, args = literal
        k = hash(literal)
        if k in self.seen_symbols:
            symbol = self.seen_symbols[k]
        else:
            symbol = backend.add_atom(atom_to_symbol(pred, args))
            self.seen_symbols[k] = symbol
        return symbol

    def update_solver(self, size, num_vars, num_rules):
        self.update_number_of_literals(size)
        self.update_number_of_vars(num_vars)
        self.update_number_of_rules(num_rules)

        # rules to add via Clingo's backend interface
        to_add = []
        to_add.extend(([], x) for x in self.all_ground_cons)

        new_seen_rules = set()

        # add handles for newly seen rules
        # for handle, rule in handles:
        for rule in self.all_handles:
            head, body = rule
            head_pred, head_args = head

            # print(head, body)

            if head_pred == 'seen_rule':
                new_seen_rules.add(head_args[0])
            else:
                assert(False)

            new_head = (True, head_pred, head_args)
            new_body = frozenset((True, pred, args) for pred, args in body)
            to_add.append((new_head, new_body))


        if self.settings.no_bias:
            self.bad_handles = []
        for handle in self.bad_handles:
            # if we know that rule_xyz is bad
            # we add the groundings of bad_stuff(R,ThisSize):- seen_rule(rule_xyz, R), R=0..MaxRules.
            for rule_id in range(0, self.settings.max_rules):
                h = (True, 'bad_stuff', (rule_id, size))
                b = (True, 'seen_rule', (handle, rule_id))
                new_rule = (h, (b,))
                to_add.append(new_rule)

            # we now eliminate bad stuff
            # :- seen_rule(rule_xyz,R1), bad_stuff(R2,Size), R1=0..MaxRules, R2=0..MaxRules, Size=0..ThisSize.
            for smaller_size in range(1, size+1):
                for r1 in range(1, self.settings.max_rules):
                    atom1 = (True, 'seen_rule', (handle, r1))
                    for r2 in range(1, self.settings.max_rules):
                        if r1 == r2:
                            continue
                        atom2 = (True, 'bad_stuff', (r2, smaller_size))
                        new_rule = ([], (atom1, atom2))
                        to_add.append(new_rule)

        with self.solver.backend() as backend:
            for head, body in to_add:
                head_literal = []
                if head:
                    head_literal = [self.gen_symbol(head, backend)]
                body_lits = []
                for literal in body:
                    sign, _pred, _args = literal
                    symbol = self.gen_symbol(literal, backend)
                    body_lits.append(symbol if sign else -symbol)
                backend.add_rule(head_literal, body_lits)

        # for x in set(handle for handle, rule in handles):
        self.seen_handles.update(new_seen_rules)


        # RESET SO WE DO NOT KEEP ADDING THEM
        self.all_ground_cons = set()
        self.bad_handles = set()
        self.all_handles = set()

        self.handle = iter(self.solver.solve(yield_ = True))

    def update_number_of_literals(self, size):
        # 1. Release those that have already been assigned
        for atom, truth_value in self.assigned.items():
            if atom[0] == 'size_in_literals' and truth_value:
                if atom[1] == size:
                    continue
                self.assigned[atom] = False
                symbol = clingo.Function('size_in_literals', [clingo.Number(atom[1])])
                self.solver.release_external(symbol)

        # 2. Ground the new size
        self.solver.ground([('number_of_literals', [clingo.Number(size)])])

        # 3. Assign the new size
        self.assigned[('size_in_literals', size)] = True

        # @NOTE: Everything passed to Clingo must be Symbol. Refactor after
        # Clingo updates their cffi API
        symbol = clingo.Function('size_in_literals', [clingo.Number(size)])
        self.solver.assign_external(symbol, True)

    def update_number_of_vars(self, size):
        # 1. Release those that have already been assigned
        for atom, truth_value in self.assigned.items():
            if atom[0] == 'size_in_vars' and truth_value:
                if atom[1] == size:
                    continue
                self.assigned[atom] = False
                symbol = clingo.Function('size_in_vars', [clingo.Number(atom[1])])
                self.solver.release_external(symbol)

        # 2. Ground the new size
        self.solver.ground([('number_of_vars', [clingo.Number(size)])])

        # 3. Assign the new size
        self.assigned[('size_in_vars', size)] = True

        # @NOTE: Everything passed to Clingo must be Symbol. Refactor after
        # Clingo updates their cffi API
        symbol = clingo.Function('size_in_vars', [clingo.Number(size)])
        self.solver.assign_external(symbol, True)

    def update_number_of_rules(self, size):
        # 1. Release those that have already been assigned
        for atom, truth_value in self.assigned.items():
            if atom[0] == 'size_in_rules' and truth_value:
                if atom[1] == size:
                    continue
                self.assigned[atom] = False
                symbol = clingo.Function('size_in_rules', [clingo.Number(atom[1])])
                self.solver.release_external(symbol)

        # 2. Ground the new size
        self.solver.ground([('number_of_rules', [clingo.Number(size)])])

        # 3. Assign the new size
        self.assigned[('size_in_rules', size)] = True

        # @NOTE: Everything passed to Clingo must be Symbol. Refactor after
        # Clingo updates their cffi API
        symbol = clingo.Function('size_in_rules', [clingo.Number(size)])
        self.solver.assign_external(symbol, True)


    def get_ground_rules(self, rule):
        head, body = rule
        # find bindings for variables in the rule
        assignments = self.grounder.find_bindings(rule, self.settings.max_rules, self.settings.max_vars)
        # keep only standard literals
        body = tuple(literal for literal in body if not literal.meta)
        # ground the rule for each variable assignment
        return set(ground_rule((head, body), assignment) for assignment in assignments)

    def parse_handles(self, new_handles):
        out = []
        for rule in new_handles:
            head, body = rule
            for h, b in self.get_ground_rules(rule):
                _, p, args = h
                out_h = (p, args)
                out_b = frozenset((b_pred, b_args) for _, b_pred, b_args in b)
                out.append((out_h, out_b))
        return out

    # @profile
    def constrain(self, tmp_new_cons, model):
        new_cons = set()
        debug = True
        debug = False

        # for con_type, con_prog, con_prog_ordering in tmp_new_cons:
        for xs in tmp_new_cons:
            con_type = xs[0]
            con_prog = xs[1]
            con_prog_ordering = xs[2]
            # con_prog, con_prog_ordering
            if debug and con_type != Constraint.UNSAT:
                print('')
                print('\t','--', con_type)
                for rule in order_prog(con_prog):
                    print('\t', format_rule(order_rule(rule)))
            if con_type == Constraint.SPECIALISATION:
                con_size = xs[3]
                new_rule_handles2, con = self.build_specialisation_constraint2(con_prog, con_prog_ordering, spec_size=con_size)
                self.all_handles.update(new_rule_handles2)
                new_cons.add(con)
            elif con_type == Constraint.GENERALISATION:
                con_size = xs[3]
                new_rule_handles2, con = self.build_generalisation_constraint2(con_prog, con_prog_ordering, gen_size=con_size)
                self.all_handles.update(new_rule_handles2)
                new_cons.add(con)
            elif con_type == Constraint.UNSAT:
                cons_ = self.unsat_constraint2(con_prog)
                self.new_ground_cons.update(cons_)
            elif con_type == Constraint.REDUNDANCY_CONSTRAINT1:
                bad_handle, new_rule_handles2, con = self.redundancy_constraint1(con_prog, con_prog_ordering)
                self.bad_handles.add(bad_handle)
                self.all_handles.update(new_rule_handles2)
                new_cons.add(con)
            elif con_type == Constraint.REDUNDANCY_CONSTRAINT2:
                new_rule_handles2, cons = self.redundancy_constraint2(con_prog, con_prog_ordering)
                self.all_handles.update(new_rule_handles2)
                new_cons.update(cons)
            elif con_type == Constraint.TMP_ANDY:
                new_cons.update(self.andy_tmp_con(con_prog))
            elif con_type == Constraint.BANISH:
                new_rule_handles2, con = self.build_banish_constraint(con_prog, con_prog_ordering)
                self.all_handles.update(new_rule_handles2)
                new_cons.add(con)

        self.all_ground_cons.update(self.new_ground_cons)
        ground_bodies = set()
        ground_bodies.update(self.new_ground_cons)

        for con in new_cons:
            ground_rules = self.get_ground_rules((None, con))
            for ground_rule in ground_rules:
                _ground_head, ground_body = ground_rule
                ground_bodies.add(ground_body)
                self.all_ground_cons.add(frozenset(ground_body))

        nogoods = []
        for ground_body in ground_bodies:
            nogood = []
            for sign, pred, args in ground_body:
                k = hash((sign, pred, args))
                try:
                    x = self.cached_clingo_atoms[k]
                except KeyError:
                    x = (atom_to_symbol(pred, args), sign)
                    self.cached_clingo_atoms[k] = x
                nogood.append(x)
            nogoods.append(nogood)

        # with self.settings.stats.duration('constrain_clingo'):
        for x in nogoods:
            model.context.add_nogood(x)

        self.new_ground_cons = set()

    def build_generalisation_constraint2(self, prog, rule_ordering=None, gen_size=False):
        new_handles = set()
        prog = list(prog)
        rule_index = {}
        literals = []
        recs = []
        for rule_id, rule in enumerate(prog):
            head, body = rule
            rule_var = vo_clause(rule_id)
            rule_index[rule] = rule_var

            if self.settings.single_solve:
                literals.extend(tuple(build_rule_literals(rule, rule_var)))
            else:
                is_rec = rule_is_recursive(rule)
                if is_rec:
                    recs.append((len(body), rule))
                handle = make_rule_handle(rule)
                if handle in self.seen_handles:
                    literals.append(build_seen_rule_literal(handle, rule_var))
                    if is_rec:
                        literals.append(gteq(rule_var, 1))
                else:
                    xs = self.build_seen_rule2(rule, is_rec)
                    # NEW!!
                    # self.seen_handles.update(xs)
                    new_handles.update(xs)
                    literals.extend(tuple(build_rule_literals(rule, rule_var)))
            literals.append(body_size_literal(rule_var, len(body)))

        if gen_size:
            literals.append(Literal('program_size_at_least', (gen_size,)))

        if rule_ordering:
            literals.extend(build_rule_ordering_literals(rule_index, rule_ordering))
        else:
            for k1, r1 in recs:
                r1v = rule_index[r1]
                for k2, r2 in recs:
                    r2v = rule_index[r2]
                    if k1 < k2:
                        literals.append(lt(r1v, r2v))
        return new_handles, tuple(literals)


    def build_seen_rule2(self, rule, is_rec):

        handle = make_rule_handle(rule)
        head, body = rule

        head_vars = set(head.arguments)
        body_vars = set(x for atom in body for x in atom.arguments if x not in head_vars)

        possible_values = list(range(len(head_vars), self.settings.max_vars))
        perms = list(permutations(possible_values, len(body_vars)))
        indexes = {x:i for i, x in enumerate(list(body_vars))}

        ground_head_args = tuple(range(len(head_vars)))

        out = []
        for rule_id in range(self.settings.max_rules):
            if is_rec and rule_id == 0:
                continue
            # new_head = (True, 'seen_rule', (handle, rule_id))
            new_head = ('seen_rule', (handle, rule_id))
            for xs in perms:
                new_body = []
                # new_body.append((True, 'head_literal', (rule_id, head.predicate, len(head.arguments), ground_head_args)))
                new_body.append(('head_literal', (rule_id, head.predicate, len(head.arguments), ground_head_args)))
                for atom in body:
                    new_args = []
                    for x in atom.arguments:
                        if x in head_vars:
                            v = ord(x)- ord('A')
                            new_args.append(v)
                        else:
                            new_args.append(xs[indexes[x]])
                    new_args = tuple(new_args)
                    # new_body.append((True, 'body_literal', (rule_id, atom.predicate, len(atom.arguments), new_args)))
                    new_body.append(('body_literal', (rule_id, atom.predicate, len(atom.arguments), new_args)))
                new_rule = (new_head, frozenset(new_body))
                out.append(new_rule)
        return frozenset(out)



    # @profile
    def build_specialisation_constraint2(self, prog, rule_ordering=None, spec_size=False):
        new_handles = set()
        prog = list(prog)
        rule_index = {}
        literals = []
        recs = []
        for rule_id, rule in enumerate(prog):
            head, body = rule
            rule_var = vo_clause(rule_id)
            rule_index[rule] = rule_var

            if self.settings.single_solve:
                literals.extend(tuple(build_rule_literals(rule, rule_var)))
            else:
                is_rec = rule_is_recursive(rule)
                if is_rec:
                    recs.append((len(body), rule))
                handle = make_rule_handle(rule)
                if handle in self.seen_handles:
                    literals.append(build_seen_rule_literal(handle, rule_var))
                    if is_rec:
                        literals.append(gteq(rule_var, 1))
                else:
                    xs = self.build_seen_rule2(rule, is_rec)
                    # NEW!!
                    # self.seen_handles.update(xs)
                    new_handles.update(xs)
                    literals.extend(tuple(build_rule_literals(rule, rule_var)))
            literals.append(lt(rule_var, len(prog)))
        literals.append(Literal('clause', (len(prog), ), positive = False))

        if spec_size:
            literals.append(Literal('program_size_at_least', (spec_size,)))

        if rule_ordering:
            literals.extend(build_rule_ordering_literals(rule_index, rule_ordering))
        else:
            for k1, r1 in recs:
                r1v = rule_index[r1]
                for k2, r2 in recs:
                    r2v = rule_index[r2]
                    if k1 < k2:
                        literals.append(lt(r1v, r2v))
        return new_handles, tuple(literals)

    def build_banish_constraint(self, prog, rule_ordering=None):
        new_handles = set()
        prog = list(prog)
        rule_index = {}
        literals = []
        recs = []
        for rule_id, rule in enumerate(prog):
            head, body = rule
            rule_var = vo_clause(rule_id)
            rule_index[rule] = rule_var
            if self.settings.single_solve:
                literals.extend(tuple(build_rule_literals(rule, rule_var)))
            else:
                is_rec = rule_is_recursive(rule)
                if is_rec:
                    recs.append((len(body), rule))
                handle = make_rule_handle(rule)
                if handle in self.seen_handles:
                    literals.append(build_seen_rule_literal(handle, rule_var))
                    if is_rec:
                        literals.append(gteq(rule_var, 1))
                else:
                    xs = self.build_seen_rule2(rule, is_rec)
                    new_handles.update(xs)
                    literals.extend(tuple(build_rule_literals(rule, rule_var)))
            literals.append(body_size_literal(rule_var, len(body)))
        literals.append(Literal('clause', (len(prog), ), positive=False))

        if rule_ordering:
            literals.extend(build_rule_ordering_literals(rule_index, rule_ordering))
        else:
            for k1, r1 in recs:
                r1v = rule_index[r1]
                for k2, r2 in recs:
                    r2v = rule_index[r2]
                    if k1 < k2:
                        literals.append(lt(r1v, r2v))
        return new_handles, tuple(literals)

    def andy_tmp_con(self, prog, rule_ordering={}):
    # :-
    # seen_rule(fABfCBtailAC,R1),
    # seen_rule(fABfCBtailAC,R2),
    # R1 < R2,
    # body_size(R1,2).
        for rule in prog:
            if not rule_is_recursive(rule):
                continue
            head, body = rule
            handle = make_rule_handle(rule)
            if handle not in self.seen_handles:
                continue
            rule_var1 = vo_clause(1)
            rule_var2 = vo_clause(2)
            literals = []
            literals.append(build_seen_rule_literal(handle, rule_var1))
            literals.append(build_seen_rule_literal(handle, rule_var2))
            literals.append(lt(rule_var1, rule_var2))
            literals.append(gteq(rule_var1, 1))
            literals.append(body_size_literal(rule_var1, len(body)))
            yield tuple(literals)

    # only works with single rule programs
    # if a single rule R is unsatisfiable, then for R to appear in an optimal solution H it must be the case that H has a recursive rule that does not specialise R
    def redundancy_constraint1(self, prog, rule_ordering=None):

        new_handles = set()
        literals = []

        rule_id = 0
        rule = list(prog)[0]
        head, body = rule
        rule_var = vo_clause(rule_id)
        handle = make_rule_handle(rule)
        if handle in self.seen_handles:
            literals.append(build_seen_rule_literal(handle, rule_var))
        else:
            xs = self.build_seen_rule2(rule, False)
            new_handles.update(xs)
            literals.extend(tuple(build_rule_literals(rule, rule_var)))

        # UNSURE
        # if self.settings.max_rules > 2:
        literals.append(gteq(rule_var, 1))
        literals.append(Literal('recursive_clause',(rule_var, head.predicate, len(head.arguments))))
        literals.append(Literal('num_recursive', (head.predicate, 1)))
        # else:
            # literals.append(gteq(rule_var, 1))

        return handle, new_handles, tuple(literals)

    def redundancy_constraint2(self, prog, rule_ordering=None):

        lits_num_rules = defaultdict(int)
        lits_num_recursive_rules = defaultdict(int)
        for rule in prog:
            head, _ = rule
            lits_num_rules[head.predicate] += 1
            if rule_is_recursive(rule):
                lits_num_recursive_rules[head.predicate] += 1

        recursively_called = set()
        while True:
            something_added = False
            for rule in prog:
                head, body = rule
                is_rec = rule_is_recursive(rule)
                for body_literal in body:
                    if body_literal.predicate not in lits_num_rules:
                        continue
                    if (body_literal.predicate != head.predicate and is_rec) or (head.predicate in recursively_called):
                        something_added |= not body_literal.predicate in recursively_called
                        recursively_called.add(body_literal.predicate)
            if not something_added:
                break

        new_handles = set()
        out_cons = []
        for lit in lits_num_rules.keys() - recursively_called:
            rule_index = {}
            literals = []

            for rule_id, rule in enumerate(prog):
                head, body = rule
                rule_var = vo_clause(rule_id)
                rule_index[rule] = rule_var
                handle = make_rule_handle(rule)

                is_rec = rule_is_recursive(rule)
                if is_rec:
                    literals.append(gteq(rule_var, 1))
                else:
                    literals.append(lt(rule_var, 1))

                if handle in self.seen_handles:
                    literals.append(build_seen_rule_literal(handle, rule_var))
                else:
                    xs = self.build_seen_rule2(rule, is_rec)
                    # NEW!!
                    # self.seen_handles.update(xs)
                    new_handles.update(xs)
                    literals.extend(tuple(build_rule_literals(rule, rule_var)))

            for other_lit, num_clauses in lits_num_rules.items():
                if other_lit == lit:
                    continue
                literals.append(Literal('num_clauses', (other_lit, num_clauses)))
            num_recursive = lits_num_recursive_rules[lit]
            literals.append(Literal('num_recursive', (lit, num_recursive)))
            if rule_ordering != None:
                literals.extend(build_rule_ordering_literals(rule_index, rule_ordering))
            out_cons.append(tuple(literals))

            # print(':- ' + ', '.join(map(str,literals)))

        return new_handles, out_cons

    # def redundant_rules_check(self, rule1, rule2):-

    def unsat_constraint2(self, body):
        assignments = self.grounder.find_deep_bindings4(body, self.settings.max_rules, self.settings.max_vars)
        out = []
        for rule_id in range(0, self.settings.max_rules):
            for assignment in assignments:
                rule = []
                for atom in body:
                    args2 = tuple(assignment[x] for x in atom.arguments)
                    rule.append((True, 'body_literal', (rule_id, atom.predicate, len(atom.arguments), args2)))
                out.append(frozenset(rule))
        return out




BINDING_ENCODING = """\
#defined rule_var/2.
#show bind_rule/2.
#show bind_var/3.

% bind a rule_id to a value
{bind_rule(Rule,Value)}:-
    rule(Rule),
    Value=0..max_rules-1.
{bind_var(Rule,Var,Value)}:-
    rule_var(Rule,Var),
    Value=0..max_vars-1.

% every rule must be bound to exactly one value
:-
    rule(Rule),
    #count{Value: bind_rule(Rule,Value)} != 1.
% for each rule, each var must be bound to exactly one value
:-
    rule_var(Rule,Var),
    #count{Value: bind_var(Rule,Var,Value)} != 1.
% a rule value cannot be bound to more than one rule
:-
    Value=0..max_rules-1,
    #count{Rule : bind_rule(Rule,Value)} > 1.
% a var value cannot be bound to more than one var per rule
:-
    rule(Rule),
    Value=0..max_vars-1,
    #count{Var : bind_var(Rule,Var,Value)} > 1.
"""



# def vo_variable(variable):
    # return ConstVar(f'{variable}', 'Variable')

def vo_variable2(rule, variable):
    key = f'{rule.name}_V{variable}'
    return VarVar(rule=rule, name=key)

def vo_clause(variable):
    return RuleVar(name=f'R{variable}')

def alldiff(args):
    return Literal('AllDifferent', args, meta=True)

def lt(a, b):
    return Literal('<', (a,b), meta=True)

def eq(a, b):
    return Literal('==', (a,b), meta=True)

def gteq(a, b):
    return Literal('>=', (a,b), meta=True)

def body_size_literal(clause_var, body_size):
    return Literal('body_size', (clause_var, body_size))

def alldiff(args):
    return Literal('AllDifferent', args, meta=True)


BODY_VARIANT_ENCODING = """\
#show bind_var/2.
value_type(Var,Type):- known_value(Var, Type).
value_type(Value,Type):- bind_var(Var,Value), var_type(Var,Type).
1 {bind_var(Var,Value): value(Value)} 1:- var(Var).
:- value(Value), #count{T : value_type(Value,T)} > 1.
:- value(Value), #count{Var : bind_var(Var,Value)} > 1.
value(V):- V=0..max_vars-1.
"""

class Grounder():
    def __init__(self, settings):
        self.seen_assignments = {}
        self.seen_deep_assignments = {}
        self.settings = settings
        self.cached4 = {}

    def find_bindings(self, rule, max_rules, max_vars):

        _, body = rule

        all_vars = find_all_vars(body)

        k = grounding_hash(body, all_vars)
        if k in self.seen_assignments:
            return self.seen_assignments[k]

        # map each rule and var_var in the program to an integer
        rule_var_to_int = {v:i for i, v in enumerate(var for var in all_vars if isinstance(var, RuleVar))}

        # transpose for later lookup
        int_to_rule_var = {i:v for v,i in rule_var_to_int.items()}

        # find all variables for each rule
        rule_vars = {k:set() for k in rule_var_to_int}
        for var in all_vars:
            if isinstance(var, VarVar):
                rule_vars[var.rule].add(var)

        encoding = []
        encoding.append(BINDING_ENCODING)
        encoding.append(f'#const max_rules={max_rules}.')
        encoding.append(f'#const max_vars={max_vars}.')

        int_lookup = {}
        tmp_lookup = {}
        for rule_var, xs in rule_vars.items():
            rule_var_int = rule_var_to_int[rule_var]
            encoding.append(f'rule({rule_var_int}).')

            for var_var_int, var_var in enumerate(xs):
                encoding.append(f'rule_var({rule_var_int},{var_var_int}).')
                int_lookup[(rule_var_int, var_var_int)] = var_var
                tmp_lookup[(rule_var, var_var)] = var_var_int
                # rule_var_lookup[(rule_var, i)] = var
                # rule_var_to_int[var] = i

        # rule_var_lookup[(rule_var, i)] = var
        # rule_var_to_int[var] = i
        # add constraints to the ASP program based on the AST thing
        for lit in body:
            if not lit.meta:
                continue
            if lit.predicate == '==':
                # pass
                var, value = lit.arguments
                rule_var = var.rule
                rule_var_int = rule_var_to_int[rule_var]
                var_var_int = tmp_lookup[(rule_var, var)]
                encoding.append(f':- not bind_var({rule_var_int},{var_var_int},{value}).')
            elif lit.predicate == '>=':
                var, val = lit.arguments
                rule_var_int1 = rule_var_to_int[var]
                # var = c_vars[var]
                # for i in range(val):
                # encoding.append(f':- c_var({var},{i}).')
                encoding.append(f':- bind_rule({rule_var_int1},Val1), Val1 < {val}.')
            elif lit.predicate == '<':
                a, b = lit.arguments
                if isinstance(b, int):
                # ABSOLUTE HACK
                    rule_var_int1 = rule_var_to_int[a]
                    encoding.append(f':- bind_rule({rule_var_int1},Val1), Val1 >= {b}.')
                else:
                    rule_var_int1 = rule_var_to_int[a]
                    rule_var_int2 = rule_var_to_int[b]
                    encoding.append(f':- bind_rule({rule_var_int1},Val1), bind_rule({rule_var_int2},Val2), Val1>=Val2.')

        encoding = '\n'.join(encoding)

        # print(encoding)

        # print('ASDASDA')
        # solver = clingo.Control()
        solver = clingo.Control(['-Wnone'])
        # solver = clingo.Control(["-t4"])
        # ask for all models
        solver.configuration.solve.models = 0
        solver.add('base', [], encoding)
        solver.ground([("base", [])])

        out = []

        def on_model(m):
            xs = m.symbols(shown = True)
            # map a variable to a program variable
            # print('xs', xs)
            assignment = {}
            for x in xs:
                name = x.name
                args = x.arguments
                if name == 'bind_var':
                    rule_var_int = args[0].number
                    var_var_int = args[1].number
                    value = args[2].number
                    var_var = int_lookup[(rule_var_int, var_var_int)]
                    assignment[var_var] = value
                else:
                    rule_var_int = args[0].number
                    value = args[1].number
                    rule_var = int_to_rule_var[rule_var_int]
                    assignment[rule_var] = value
            out.append(assignment)
        solver.solve(on_model=on_model)
        self.seen_assignments[k] = out
        return out


    def find_deep_bindings4(self, body, max_rules, max_vars):
        all_vars = set(x for atom in body for x in atom.arguments)
        head_types = self.settings.head_types
        body_types = self.settings.body_types

        var_type_lookup = {}
        var_to_index = {}
        index_to_var = {}

        # MAP A->0, B->1
        for x in all_vars:
            k = ord(x)- ord('A')
            var_to_index[x] = k
            index_to_var[k] = x

        head_vars = set()
        if head_types:
            for k, head_type in enumerate(head_types):
                var_type_lookup[k] = head_type
                head_vars.add(k)

        body_vars = set()
        for atom in body:
            pred = atom.predicate
            if pred not in body_types:
                continue
            for i, x in enumerate(atom.arguments):
                k = ord(x)- ord('A')
                body_vars.add(k)
                var_type = body_types[pred][i]
                var_type_lookup[k] = var_type

        # if cache:
        if body_vars:
            key = hash(frozenset((k,v) for k,v in var_type_lookup.items() if k in body_vars))
        else:
            key = hash(frozenset(all_vars))
        if key in self.cached4:
            return self.cached4[key]

        formula = CNF()
        bad_ks = set()
        for x in body_vars:
            if x not in var_type_lookup:
                continue
            for y in head_vars:
                if x == y:
                    continue
                if y not in var_type_lookup:
                    continue
                if var_type_lookup[x] == var_type_lookup[y]:
                    continue
                k = (x, y)
                bad_ks.add(k)

        solver_vars = list(var_to_index.values())
        solver_values = list(range(0, max_vars))
        var_lookup = {}
        solver_index = {}
        index = 1

        for x in solver_vars:
            x_clause = []
            for y in solver_values:
                # match x to y
                k = (x,y)
                if k in bad_ks:
                    continue
                var_lookup[k] = index
                solver_index[index] = k
                index+=1
                x_clause.append(var_lookup[k])
            formula.append(x_clause)
            for z in CardEnc.equals(lits=x_clause, encoding=EncType.pairwise).clauses:
                formula.append(z)

        for y in solver_values:
            y_clause = []
            for x in solver_vars:
                k = (x,y)
                if k in bad_ks:
                    continue
                y_clause.append(var_lookup[k])
            for z in CardEnc.atmost(lits=y_clause, encoding=EncType.pairwise).clauses:
                formula.append(z)

        solver = Solver(name='m22')
        for x in formula.clauses:
            solver.add_clause(x)

        out = []
        for m in solver.enum_models():
            assignment = {}
            for x in m:
                if x < 0:
                    continue
                x, y = solver_index[x]
                assignment[index_to_var[x]] = y
            out.append(assignment)

        # if cache:
        self.cached4[key] = out
        return out

        # solver.solve(on_model=on_model)
        # return out
