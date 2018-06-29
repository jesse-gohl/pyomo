#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and 
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain 
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

import logging
logger = logging.getLogger('pyomo.core')

from six import next, iteritems, itervalues
from collections import OrderedDict

from pyomo.core.expr import current as EXPR
from pyomo.core.kernel import ComponentMap, ComponentSet
from pyomo.core.base.plugin import alias
from pyomo.core.base import Transformation, Connector, Constraint, \
    ConstraintList, Var, VarList, Connection, Block, SortComponents
from pyomo.core.base.connector import _ConnectorData, SimpleConnector
from pyomo.core.base.connection import SimpleConnection
from pyomo.common.modeling import unique_component_name


class _ConnExpansion(Transformation):
    def _collect_connectors(self, instance, ctype):
        self._name_buffer = {}
        # List of the connectors in the order in which we found them
        # (this should be deterministic, provided that the user's model
        # is deterministic)
        connector_list = []
        # list of constraints with connectors: tuple(constraint, connector_set)
        # (this should be deterministic, provided that the user's model
        # is deterministic)
        constraint_list = []
        # analogous to constraint_list
        connection_list = []
        # ID of the next connector group (set of matched connectors)
        groupID = 0
        # connector_groups stars out as a dict of {id(set): (groupID, set)}
        # If you sort by the groupID, then this will be deterministic.
        connector_groups = dict()
        # map of connector to the set of connectors that must match it
        matched_connectors = ComponentMap()
        # The set of connectors found in the current component
        found = ComponentSet()

        connector_types = set([SimpleConnector, _ConnectorData])
        for comp in instance.component_data_objects(
                ctype, sort=SortComponents.deterministic, active=True):
            if comp.type() is Constraint:
                itr = EXPR.identify_components(comp.body, connector_types)
            else: # Connection
                itr = comp.connectors
            ref = None
            for c in itr:
                found.add(c)
                if c in matched_connectors:
                    if ref is None:
                        # The first connector in this comp has
                        # already been seen.  We will use that Set as
                        # the reference
                        ref = matched_connectors[c]
                    elif ref is not matched_connectors[c]:
                        # We already have a reference group; merge this
                        # new group into it.

                        # Optimization: this merge is linear in the size
                        # of the src set.  If the reference set is
                        # smaller, save time by switching to a new
                        # reference set.
                        src = matched_connectors[c]
                        if len(ref) < len(src):
                            ref, src = src, ref
                        ref.update(src)
                        for i in src:
                            matched_connectors[i] = ref
                        del connector_groups[id(src)]
                    # else: pass
                    #   The new group *is* the reference group;
                    #   there is nothing to do.
                else:
                    # The connector has not been seen before.
                    connector_list.append(c)
                    if ref is None:
                        # This is the first connector in the comp:
                        # start a new reference set.
                        ref = ComponentSet()
                        connector_groups[id(ref)] = (groupID, ref)
                        groupID += 1
                    # This connector hasn't been seen.  Record it.
                    ref.add(c)
                    matched_connectors[c] = ref
            if ref is not None:
                if comp.type() is Constraint:
                    constraint_list.append( (comp, found) )
                else:
                    connection_list.append( (comp, found) )
                found = ComponentSet()

        # Validate all connector sets and expand the empty ones
        known_conn_sets = {}
        for groupID, conn_set in sorted(itervalues(connector_groups)):
            known_conn_sets[id(conn_set)] \
                = self._validate_and_expand_connector_set(conn_set)

        return (constraint_list, connection_list, connector_list,
                matched_connectors, known_conn_sets)

    def _validate_and_expand_connector_set(self, connectors):
        ref = {}
        # First, go through the connectors and get the superset of all fields
        for c in connectors:
            for k,v in iteritems(c.vars):
                if k in ref:
                    # We have already seen this var
                    continue
                if v is None:
                    # This is an implicit var
                    continue
                # OK: New var, so add it to the reference list
                _len = (
                    #-3 if v is None else
                    -2 if k in c.aggregators else
                    -1 if not hasattr(v, 'is_indexed') or not v.is_indexed()
                    else len(v) )
                ref[k] = ( v, _len, c )

        if not ref:
            logger.warning(
                "Cannot identify a reference connector: no connectors "
                "in the connector set have assigned variables:\n\t(%s)"
                % ', '.join(sorted(c.name for c in itervalues(connectors))))
            return ref

        # Now make sure that connectors match
        empty_or_partial = []
        for c in connectors:
            c_is_partial = False
            if not c.vars:
                # This is an empty connector and should be defined with
                # "auto" vars
                empty_or_partial.append(c)
                continue

            for k,v in iteritems(ref):
                if k not in c.vars:
                    raise ValueError(
                        "Connector mismatch: Connector '%s' missing variable "
                        "'%s' (appearing in reference connector '%s')" %
                        ( c.name, k, v[2].name ) )
                _v = c.vars[k]
                if _v is None:
                    if not c_is_partial:
                        empty_or_partial.append(c)
                        c_is_partial = True
                    continue
                _len = (
                    -3 if _v is None else
                    -2 if k in c.aggregators else
                    -1 if not hasattr(_v, 'is_indexed') or not _v.is_indexed()
                    else len(_v) )
                if (_len >= 0) ^ (v[1] >= 0):
                    raise ValueError(
                        "Connector mismatch: Connector variable '%s' mixing "
                        "indexed and non-indexed targets on connectors '%s' "
                        "and '%s'" %
                        ( k, v[2].name, c.name ))
                if _len >= 0 and _len != v[1]:
                    raise ValueError(
                        "Connector mismatch: Connector variable '%s' index "
                        "mismatch (%s elements in reference connector '%s', "
                        "but %s elements in connector '%s')" %
                        ( k, v[1], v[2].name, _len, c.name ))
                if v[1] >= 0 and len(v[0].index_set() ^ _v.index_set()):
                    raise ValueError(
                        "Connector mismatch: Connector variable '%s' has "
                        "mismatched indices on connectors '%s' and '%s'" %
                        ( k, v[2].name, c.name ))


        # as we are adding things to the model, sort by key so that
        # the order things are added is deterministic
        sorted_refs = sorted(iteritems(ref))
        if len(empty_or_partial) > 1:
            # This is expensive (names aren't cheap), but does result in
            # a deterministic ordering
            empty_or_partial.sort(key=lambda x: x.getname(
                fully_qualified=True, name_buffer=self._name_buffer))

        # Fill in any empty connectors
        for c in empty_or_partial:
            block = c.parent_block()
            for k, v in sorted_refs:
                if k in c.vars and c.vars[k] is not None:
                    continue

                if v[1] >= 0:
                    idx = ( v[0].index_set(), )
                else:
                    idx = ()
                var_args = {}
                try:
                    var_args['domain'] = v[0].domain
                except AttributeError:
                    pass
                try:
                    var_args['bounds'] = v[0].bounds
                except AttributeError:
                    pass
                new_var = Var( *idx, **var_args )
                vname = '%s.auto.%s' % (c.getname(
                    fully_qualified=True, name_buffer=self._name_buffer), k)
                block.add_component(vname, new_var)
                if idx:
                    for i in idx[0]:
                        new_var[i].domain = v[0][i].domain
                        new_var[i].setlb( v[0][i].lb )
                        new_var[i].setub( v[0][i].ub )
                c.vars[k] = new_var

        return ref

    def _build_connections(self, connection_list, matched_connectors,
                           known_conn_sets):
        indexed_ctns = OrderedDict() # maintain deterministic order we have
        for ctn, conn_set in connection_list:
            if not isinstance(ctn, SimpleConnection):
                # create indexed blocks later for indexed connections
                lst = indexed_ctns.get(ctn.parent_component(), [])
                lst.append( (ctn, conn_set) )
                indexed_ctns[ctn.parent_component()] = lst
                continue
            blk = Block()
            bname = unique_component_name(
                ctn.parent_block(), "%s_expanded" % ctn.getname(
                    fully_qualified=False, name_buffer=self._name_buffer))
            ctn.parent_block().add_component(bname, blk)
            # add reference to this block onto the Connection object
            ctn._expanded_block = blk
            self._add_connections(
                blk, conn_set, matched_connectors, known_conn_sets)
            ctn.deactivate()

        for ictn in indexed_ctns:
            blk = Block(ictn.index_set())
            bname = unique_component_name(
                ictn.parent_block(), "%s_expanded" % ictn.getname(
                    fully_qualified=False, name_buffer=self._name_buffer))
            ictn.parent_block().add_component(bname, blk)
            ictn._expanded_block = blk
            for ctn, conn_set in indexed_ctns[ictn]:
                i = ctn.index()
                self._add_connections(
                    blk[i], conn_set, matched_connectors, known_conn_sets)
            ictn.deactivate()

    def _add_connections(self, blk, conn_set, matched_connectors,
                         known_conn_sets):
        if len(conn_set) == 1:
            # possible to have a connection equating a connector to itself
            # emit the trivial constraint, as opposed to skipping it
            # conn_set is a set, so make a list that contains itself repeated
            conn_set = [k for k in conn_set] * 2
        conn = next(iter(conn_set))
        ref = known_conn_sets[id(matched_connectors[conn])]
        for k, v in sorted(iteritems(ref)):
            # if one of them is extensive, make the new variable
            # if both are, skip the constraint since both use the same var
            # name is k, conflicts are prevented by a check in add function
            # the new var will mirror the original var and have same index set
            cont = once = False
            for c in conn_set:
                for etype in c.extensives:
                    if k in c.extensives[etype]:
                        if once:
                            cont = True
                            c.extensives[etype][k].append(evar)
                        else:
                            once = True
                            evar = Var(c.vars[k].index_set())
                            blk.add_component(k, evar)
                            c.extensives[etype][k].append(evar)
                        break
            if cont:
                continue

            cname = k + "_equality"
            def rule(m, *args):
                if len(args) == 0:
                    # scalar, use None as index
                    args = None
                tmp = []
                for c in conn_set:
                    if k in c.aggregators:
                        tmp.append(c.vars[k].add())
                    elif k in c.extensives:
                        tmp.append(evar)
                    else:
                        tmp.append(c.vars[k][args])
                return tmp[0] == tmp[1]
            con = Constraint(v[0].index_set(), rule=rule)
            blk.add_component(cname, con)

    def _implement_aggregators(self, connector_list):
        for conn in connector_list:
            block = conn.parent_block()
            for var, aggregator in iteritems(conn.aggregators):
                c = Constraint(expr=aggregator(block, conn.vars[var]))
                cname = '%s.%s.aggregate' % (conn.getname(
                    fully_qualified=True, name_buffer=self._name_buffer), var)
                block.add_component(cname, c)

    def _implement_extensives(self, connector_list):
        for ctr in connector_list:
            unit = ctr.parent_block()
            for etype in ctr.extensives:
                if etype not in c.extensive_aggregators:
                    raise KeyError(
                        "No aggregator in extensive_aggregators for extensive "
                        "type '%s' in Connector '%s'" % (etype, ctr.name))
                fcn = ctr.extensive_aggregators[etype]
                # build list of connections using the parent blocks of all
                # the evars in one of the lists in ctr.extensives[etype]
                ctns = [evar.parent_block() for evar in
                        next(itervalues(ctr.extensives[etype]))]
                fcn(unit, ctns, ctr, etype)


class ExpandConnectors(_ConnExpansion):
    alias('core.expand_connectors',
          doc="Expand all connectors in the model to simple constraints")

    def _apply_to(self, instance, **kwds):
        if __debug__ and logger.isEnabledFor(logging.DEBUG):   #pragma:nocover
            logger.debug("Calling ConnectorExpander")

        connectorsFound = False
        for c in instance.component_data_objects(Connector):
            connectorsFound = True
            break
        if not connectorsFound:
            return

        if __debug__ and logger.isEnabledFor(logging.DEBUG):   #pragma:nocover
            logger.debug("   Connectors found!")

        #
        # At this point, there are connectors in the model, so we must
        # look for constraints that involve connectors and expand them.
        #
        (constraint_list, connection_list, connector_list, matched_connectors,
            known_conn_sets) = self._collect_connectors(instance,
            (Constraint, Connection))

        # Expand each constraint
        for constraint, conn_set in constraint_list:
            cList = ConstraintList()
            cname = '%s.expanded' % constraint.getname(
                fully_qualified=False, name_buffer=self._name_buffer)
            constraint.parent_block().add_component(cname, cList)
            connId = next(iter(conn_set))
            ref = known_conn_sets[id(matched_connectors[connId])]
            for k,v in sorted(iteritems(ref)):
                if v[1] >= 0:
                    _iter = v[0]
                else:
                    _iter = (v[0],)
                for idx in _iter:
                    substitution = {}
                    for c in conn_set:
                        if v[1] >= 0:
                            new_v = c.vars[k][idx]
                        elif k in c.aggregators:
                            new_v = c.vars[k].add()
                        else:
                            new_v = c.vars[k]
                        substitution[id(c)] = new_v
                    cList.add((
                        constraint.lower,
                        EXPR.clone_expression( constraint.body, substitution ),
                        constraint.upper ))
            constraint.deactivate()

        self._build_connections(connection_list, matched_connectors,
            known_conn_sets)

        # Now, go back and implement VarList aggregators
        self._implement_aggregators(connector_list)


class ExpandConnections(_ConnExpansion):
    alias('core.expand_connections',
          doc="Expand all Connections in the model to simple constraints")

    def _apply_to(self, instance, **kwds):
        if __debug__ and logger.isEnabledFor(logging.DEBUG):   #pragma:nocover
            logger.debug("Calling ConnectionExpander")

        # need to collect all connectors to see every connector each
        # is related to so that we can expand empty connectors
        (_, connection_list, connector_list, matched_connectors,
            known_conn_sets) = self._collect_connectors(instance, Connection)

        self._build_connections(connection_list, matched_connectors,
            known_conn_sets)

        # Now, go back and implement aggregators
        self._implement_aggregators(connector_list)
        self._implement_extensives(connector_list)
