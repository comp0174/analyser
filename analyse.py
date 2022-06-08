import os
from tempfile import TemporaryDirectory
from pathlib import Path
import csv
from subprocess import run, DEVNULL, PIPE
import itertools
import argparse
import graphviz
from pycparser import parse_file
from pycparser import c_generator, c_ast


class UnsupportedLanguageConstruct(Exception):
    pass


def is_deref(n):
    if n.__class__.__name__ == "UnaryOp" and \
       n.op == "*" and \
       n.expr.__class__.__name__ == "ID":
        return n.expr.name
    else:
        return None


def is_address(n):
    if n.__class__.__name__ == "UnaryOp" and \
       n.op == "&" and \
       n.expr.__class__.__name__ == "ID":
        return n.expr.name
    else:
        return None


def is_var(n):
    if n.__class__.__name__ == "ID":
        return n.name
    else:
        return None


def is_const(n):
    if n.__class__.__name__ == "Constant":
        return str(n.value)
    else:
        return None


class ExpressionVisitor:

    def __init__(self):
        self.variables = set()
        self.deref_variables = set()

    def visit(self, node):
        method = 'visit_' + node.__class__.__name__
        return getattr(self, method, self.generic_visit)(node)

    def generic_visit(self, node):
        raise UnsupportedLanguageConstruct(node.__class__.__name__)

    def visit_Constant(self, n):
        pass

    def visit_ID(self, n):
        self.variables.add(n.name)

    def visit_UnaryOp(self, n):
        if is_deref(n):
            self.deref_variables.add(is_deref(n))
        else:
            self.visit(n.expr)

    def visit_BinaryOp(self, n):
        # ignore n.op
        self.visit(n.left)
        self.visit(n.right)
    

class StatementVisitor:

    def __init__(self):
        self._c_generator = c_generator.CGenerator()
        self._availabe_label_index = 1
        self.cfg = graphviz.Digraph('control_flow_graph', filename='cfg.gv')
        self.cfg.attr('node', shape='box')
        self.edb = {}
        self.edb["flow"] = []
        self.edb["label"] = []
        self.edb["variable"] = []
        self.edb["assignment"] = []
        self.edb["condition"] = []
        self.edb["return"] = []
        self.edb["call"] = []
        self.edb["function"] = []
        self.edb["used"] = []
        self.edb["defined"] = []
        self.edb["defined_deref"] = []
        self.edb["used_deref"] = []
        self.edb["rhs_var"] = []
        self.edb["rhs_const"] = []
        self.edb["rhs_deref"] = []
        self.edb["rhs_address"] = []
        self.edb["call_arg_var"] = []
        self.edb["call_arg_const"] = []
        self.edb["call_arg_deref"] = []

    def _new_label(self):
        id = "l" + str(self._availabe_label_index)
        self._availabe_label_index += 1
        self.edb["label"].append(id)
        return id

    def _add_elementary_block(self, n):
        """adds an elementary block to CFG, EDB; returns the label"""
        label = self._new_label()
        self.cfg.node(label,
                       label=self._c_generator.visit(n),
                       xlabel=label)
        return label

    def _add_arc(self, source, destination, arc_label=None):
        if arc_label:
            self.cfg.edge(source, destination, label=arc_label)
        else:
            self.cfg.edge(source, destination)
        self.edb["flow"].append((source, destination))

    def _process_expr(self, n):
        v = ExpressionVisitor()
        v.visit(n)
        for var in itertools.chain(v.variables, v.deref_variables):
            if var not in self.edb["variable"]:
                self.edb["variable"].append(var)
        return (v.variables, v.deref_variables)

    def _add_used(self, vars, deref_vars, label):
        for v in set([*vars, *deref_vars]):
            self.edb["used"].append((v, label))
        for v in set(deref_vars):
            self.edb["used_deref"].append((v, label))

    def visit(self, node):
        method = 'visit_' + node.__class__.__name__
        return getattr(self, method, self.generic_visit)(node)

    def generic_visit(self, node):
        raise UnsupportedLanguageConstruct(node.__class__.__name__)

    def visit_FileAST(self, n):
        if len(n.ext) == 0:
            raise UnsupportedLanguageConstruct("empty file")
        if len(n.ext) > 1:
            raise UnsupportedLanguageConstruct("more than one top-level element")
        if isinstance(n.ext[0], c_ast.FuncDef):
            return self.visit(n.ext[0])
        else:
            raise UnsupportedLanguageConstruct(node.__class__.__name__)

    def visit_FuncDef(self, n):
        assert(n.decl.name == 'main')
        return self.visit(n.body)

    def visit_Compound(self, n):
        head, *tail = n.block_items
        head_init, head_finals = self.visit(head)
        prev_finals = head_finals
        for stmt in tail:
            cur_init, cur_finals = self.visit(stmt)
            for prev in prev_finals:
                self._add_arc(prev, cur_init)
            prev_finals = cur_finals
        return head_init, prev_finals

    def visit_If(self, n):
        if n.cond is None:
            raise UnsupportedLanguageConstruct("empty condition")

        cond_label = self._add_elementary_block(n.cond)
        self.edb["condition"].append(cond_label)        
        if_finals = []
        cond_vars, cond_deref_vars = self._process_expr(n.cond)
        self._add_used(cond_vars, cond_deref_vars, cond_label)
        if n.iftrue is None and n.iffalse is None:
            raise UnsupportedLanguageConstruct("if without branches")

        if n.iftrue is not None:
            iftrue_init, iftrue_finals = self.visit(n.iftrue)
            self._add_arc(cond_label, iftrue_init, 'true')
            if_finals.extend(iftrue_finals)
        else:
            if_finals.append(cond_label)

        if n.iffalse is not None:
            iffalse_init, iffalse_finals = self.visit(n.iffalse)
            self._add_arc(cond_label, iffalse_init, 'false')
            if_finals.extend(iffalse_finals)
        else:
            if_finals.append(cond_label)

        return (cond_label, if_finals)

    def visit_FuncCall(self, n):
        label = self._add_elementary_block(n)
        if n.name.name not in self.edb["function"]:
            self.edb["function"].append(n.name.name)
        self.edb["call"].append((n.name.name, label))
        if n.args:
            args = list(n.args)
            for arg in args:
                arg_vars, arg_deref_vars = self._process_expr(arg)
                self._add_used(arg_vars, arg_deref_vars, label)
            if len(args) == 1:
                if is_deref(args[0]):
                    self.edb["call_arg_deref"].append((n.name.name, is_deref(args[0]), label))
                if is_var(args[0]):
                    self.edb["call_arg_var"].append((n.name.name, is_var(args[0]), label))
                if is_const(args[0]):
                    self.edb["call_arg_const"].append((n.name.name, is_const(args[0]), label))
        return (label, [label])

    def visit_While(self, n):
        if n.cond is None:
            raise UnsupportedLanguageConstruct("empty condition")

        cond_label = self._add_elementary_block(n.cond)
        self.edb["condition"].append(cond_label)
        cond_vars, cond_deref_vars = self._process_expr(n.cond)
        self._add_used(cond_vars, cond_deref_vars, cond_label)

        if n.stmt is not None:
            body_init, body_finals = self.visit(n.stmt)
            self._add_arc(cond_label, body_init, 'true')
            for final in body_finals:
                self._add_arc(final, cond_label)

        return (cond_label, [cond_label])

    def visit_Assignment(self, n):
        label = self._add_elementary_block(n)
        rvalue_vars, rvalue_deref_vars = self._process_expr(n.rvalue)
        self._add_used(rvalue_vars, rvalue_deref_vars, label)
        if is_deref(n.rvalue):
            self.edb["rhs_deref"].append((is_deref(n.rvalue), label))
        if is_address(n.rvalue):
            self.edb["rhs_address"].append((is_address(n.rvalue), label))
        if is_var(n.rvalue):
            self.edb["rhs_var"].append((is_var(n.rvalue), label))
        if is_const(n.rvalue):
            self.edb["rhs_const"].append((is_const(n.rvalue), label))
        lvalue_vars, lvalue_deref_vars = self._process_expr(n.lvalue)
        if len(lvalue_vars) == 1 and len(lvalue_deref_vars) == 0:
            self.edb["defined"].append((list(lvalue_vars)[0], label))
        elif len(lvalue_vars) == 0 and len(lvalue_deref_vars) == 1:
            self.edb["defined_deref"].append((list(lvalue_deref_vars)[0], label))
        else:
            raise UnsupportedLanguageConstruct(n)
        self.edb["assignment"].append(label)
        return (label, [label])

    def visit_Return(self, n):
        label = self._add_elementary_block(n)
        expr_vars, expr_deref_vars = self._process_expr(n.expr)
        self._add_used(expr_vars, expr_deref_vars, label)
        self.edb["return"].append(label)
        return (label, [])
    

def generate_cfg(ast):
    v = StatementVisitor()
    init, finals = v.visit(ast)
    v.edb["init"] = [init]
    v.edb["final"] = list(set([*finals, *v.edb["return"]]))
    return (v.cfg, v.edb)


def load_relations(directory):
    """returns mapping from relation name to a list of tuples"""
    relations = dict()
    for file in itertools.chain(Path(directory).glob('*.facts'),
                                Path(directory).glob('*.csv')):
        relation_name = file.stem
        with open(file) as csvfile:
            reader = csv.reader(csvfile, delimiter='\t')
            relations[relation_name] = list(reader)
    return relations


def write_relations(directory, relations):
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    for relation_name, tuples in relations.items():
        file = d / (relation_name + '.facts')
        with file.open(mode='w') as file:
            writer = csv.writer(file, delimiter='\t')
            for tuple in tuples:
                if isinstance(tuple, str):
                    writer.writerow((tuple,))
                else:
                    writer.writerow(tuple)


def check_relations(relations, datalog_script):
    with TemporaryDirectory() as input_directory:
        write_relations(input_directory, relations)
        with TemporaryDirectory() as output_directory:
            cmd = [
                "souffle",
                "-F", input_directory,
                "-D", output_directory,
                datalog_script
            ]
            run(cmd, check=True, stdout=DEVNULL, stderr=DEVNULL)
            return load_relations(output_directory)


def pprint(tuples):
    result = []
    for tuple in tuples:
        args_comma_sep = ', '.join(['"' + a + '"' for a in tuple])
        result.append('  [' + args_comma_sep + ']')
    return '[\n' + ',\n'.join(result) + '\n]\n'


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='COMP0174 Analyser.')
    parser.add_argument('file', metavar='FILE', help='a file to analyse')
    parser.add_argument('--analysis', metavar='FILE', help='analysis file')
    parser.add_argument('--output-edb', metavar='DIR', help='output directory')

    args = parser.parse_args()
    ast = parse_file(args.file, use_cpp=True)
    cfg, edb = generate_cfg(ast)
    if args.output_edb:
        write_relations(args.output_edb, edb)
        cfg.render(directory=args.output_edb)
    elif args.analysis:
        script_dir = Path(os.path.dirname(os.path.realpath(__file__)))
        datalog_script = args.analysis
        output = check_relations(edb, datalog_script)
        for relation, tuples in output.items():
            if relation == 'result':
                print(pprint(tuples))
        
    
