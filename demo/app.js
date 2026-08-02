/* Demo page wiring. Everything here runs against the real demo engine. */

'use strict';

(function () {
  const E = window.CanonEngine;
  const D = window.CanonData;

  const ruleset = new E.RuleSet('crew_rostering', '2026.08.1', D.RULES, D.DERIVED_POLICY);
  let enabled = D.RULES.map((r) => r.id);

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const bytes = (n) => (n < 1024 ? n + ' B' : (n / 1024).toFixed(1) + ' kB');
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));
  const setPlain = (selector, text) => {
    const node = $(selector);
    if (node) node.textContent = text;
  };

  /* ================================================================== */
  /* Section: payload contract                                           */
  /* ================================================================== */

  function renderTree(tree, container, prefix) {
    const keys = Object.keys(tree).sort();
    keys.forEach((key) => {
      const node = tree[key];
      const children = Object.keys(node.children);
      const line = el('div', 'tree-line');
      line.style.paddingLeft = (prefix * 16) + 'px';
      const name = el('span', 'tree-key', key + (node.collection ? '[*]' : ''));
      line.appendChild(name);
      if (!children.length) line.appendChild(el('span', 'tree-leaf', 'value'));
      container.appendChild(line);
      if (children.length) renderTree(node.children, container, prefix + 1);
    });
  }

  function updateContract() {
    const client = $('#contract-client').value || null;
    const asOf = $('#contract-date').value || null;
    const scenarioKey = $('#contract-scenario').value;
    const scenario = D.SCENARIOS[scenarioKey];

    const projection = ruleset.projectionFor(client, asOf, enabled);
    const applicable = ruleset.applicable(client, asOf, enabled);

    const treeBox = $('#contract-tree');
    treeBox.innerHTML = '';
    renderTree(projection.tree, treeBox, 0);

    const full = JSON.stringify(scenario.facts);
    const trimmed = JSON.stringify(E.selectByProjection(projection.tree, scenario.facts));
    const saved = full.length ? (1 - trimmed.length / full.length) : 0;

    $('#contract-rules').textContent = applicable.length + ' of ' + D.RULES.length;
    $('#contract-fields').textContent = projection.paths.length;
    $('#contract-full').textContent = bytes(full.length);
    $('#contract-trimmed').textContent = bytes(trimmed.length);
    $('#contract-saved').textContent = (saved * 100).toFixed(1) + '%';
    $('#contract-bar-trimmed').style.width = Math.max(2, (trimmed.length / full.length) * 100) + '%';

    const requesters = {};
    applicable.forEach((rule) => {
      ruleset.rulePaths[rule.id].forEach((path) => {
        if (!requesters[path]) requesters[path] = [];
        requesters[path].push(rule.id);
      });
    });
    const list = $('#contract-paths');
    list.innerHTML = '';
    projection.paths.forEach((path) => {
      const row = el('div', 'path-row');
      const isVertical = path.indexOf('[*]') !== -1;
      row.appendChild(el('code', 'path' + (isVertical ? ' vertical' : ''), path));
      row.appendChild(el('span', 'requesters', requesters[path].join(', ')));
      list.appendChild(row);
    });

    const verticalCount = projection.paths.filter((p) => p.indexOf('[*]') !== -1).length;
    $('#contract-vertical').textContent = verticalCount;

    setPlain('#contract-plain',
      'These ' + plural(applicable.length, 'rule') + ' need '
      + plural(projection.paths.length, 'separate fact') + ' between them. '
      + 'The message the rostering system sends today carries ' + bytes(full.length)
      + '; sending only what the rules use would carry ' + bytes(trimmed.length)
      + ', so ' + (saved * 100).toFixed(0) + '% of it never has to travel. '
      + (verticalCount
        ? plural(verticalCount, 'of those facts is', 'of those facts are')
          + ' about every crew member on the flight rather than just the one being assigned.'
        : 'None of them need anything about the rest of the crew.')
      + ' Nobody wrote this list: it comes from reading the rules.');
  }

  function buildRuleToggles() {
    const box = $('#rule-toggles');
    box.innerHTML = '';
    D.RULES.forEach((rule) => {
      const label = el('label', 'toggle');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = true;
      input.dataset.ruleId = rule.id;
      input.addEventListener('change', () => {
        enabled = $$('#rule-toggles input:checked').map((i) => i.dataset.ruleId);
        updateContract();
      });
      label.appendChild(input);
      label.appendChild(el('span', 'toggle-id', rule.id));
      label.appendChild(el('span', 'toggle-title', rule.title));
      if (rule.vertical) label.appendChild(el('span', 'chip chip-vertical', 'vertical slice'));
      if (rule.clients && rule.clients[0] !== '*') {
        label.appendChild(el('span', 'chip chip-tenant', rule.clients.join(',')));
      }
      if (rule.effectiveFrom) label.appendChild(el('span', 'chip chip-future', 'from ' + rule.effectiveFrom));
      box.appendChild(label);
    });
  }

  /* ================================================================== */
  /* Section: evaluate                                                   */
  /* ================================================================== */

  let lastDecision = null;

  function runScenario() {
    const key = $('#eval-scenario').value;
    const scenario = D.SCENARIOS[key];
    const client = $('#eval-client').value || null;
    const asOf = $('#eval-date').value || null;

    const decision = E.evaluate(ruleset, scenario.facts, { client, asOf, enabled: null });
    lastDecision = decision;

    $('#eval-blurb').textContent = scenario.blurb;
    const verdict = $('#eval-verdict');
    verdict.textContent = decision.ok ? 'PASS' : 'FAIL';
    verdict.className = 'verdict ' + (decision.ok ? 'pass' : 'fail');
    $('#eval-time').textContent = decision.micros.toFixed(0) + ' us';
    $('#eval-planned').textContent = decision.stats.plannedPaths;
    $('#eval-read').textContent = decision.stats.readPaths;
    $('#eval-roots').textContent = decision.stats.rootsFetched + ' of ' + decision.stats.rootsPlanned;
    $('#eval-digest').textContent = decision.outputDigest.slice(0, 16);

    const findings = $('#eval-findings');
    findings.innerHTML = '';
    if (!decision.findings.length) {
      findings.appendChild(el('div', 'muted', 'No findings. The assignment is legal under this ruleset.'));
    }
    decision.findings.forEach((finding) => {
      const card = el('div', 'finding ' + finding.severity);
      const head = el('div', 'finding-head');
      head.appendChild(el('span', 'sev ' + finding.severity, finding.severity));
      head.appendChild(el('code', 'code', finding.code));
      head.appendChild(el('span', 'from', finding.ruleId + ' v' + finding.ruleVersion));
      const why = el('button', 'why', 'why');
      why.addEventListener('click', () => showExplain(finding.code));
      head.appendChild(why);
      card.appendChild(head);
      card.appendChild(el('div', 'finding-msg', finding.message));
      findings.appendChild(card);
    });

    const derived = $('#eval-derived');
    derived.innerHTML = '';
    Object.keys(decision.derived).sort().forEach((name) => {
      const row = el('div', 'derived-row');
      row.appendChild(el('code', 'dname', 'derived.' + name));
      row.appendChild(el('span', 'dval', String(decision.derived[name])));
      row.appendChild(el('span', 'dpolicy', 'combine: ' + (D.DERIVED_POLICY[name] || 'first')));
      derived.appendChild(row);
    });

    renderTrace(decision);
    showExplain(decision.findings.length ? decision.findings[0].code : null);

    const hard = decision.findings.filter((f) => f.severity === 'hard');
    const soft = decision.findings.filter((f) => f.severity !== 'hard');
    const considered = decision.traces.filter((t) => t.considered).length;
    const skipped = decision.traces.length - considered;

    let plain;
    if (hard.length) {
      plain = 'This assignment is not legal. '
        + plural(hard.length, 'rule blocks', 'rules block') + ' it. '
        + 'The first is: ' + hard[0].message.trim().replace(/\.$/, '') + '.';
    } else {
      plain = 'This assignment is legal. Nothing blocks it.';
    }
    if (soft.length) {
      plain += ' There ' + (soft.length === 1 ? 'is also 1 advisory' : 'are also '
        + soft.length + ' advisories') + ' worth a planner looking at, '
        + 'but they do not stop the assignment.';
    }
    plain += ' Canon considered ' + plural(considered, 'rule')
      + (skipped ? ', skipped ' + skipped + ' that do not apply to this airline or date,' : ',')
      + ' and read ' + decision.stats.readPaths + ' of the '
      + decision.stats.plannedPaths + ' facts it might have needed. '
      + 'Every one of those reads is listed in the working on the right.';
    setPlain('#eval-plain', plain);
  }

  function renderTrace(decision) {
    const box = $('#eval-trace');
    box.innerHTML = '';
    let currentStratum = -1;
    decision.traces.forEach((trace) => {
      if (trace.stratum !== currentStratum) {
        currentStratum = trace.stratum;
        const header = el('div', 'stratum-header',
          'round ' + (currentStratum + 1)
          + ': these rules cannot see each other’s answers');
        box.appendChild(header);
      }
      const row = el('div', 'trace-row ' + (trace.considered ? (trace.fired ? 'fired' : 'quiet') : 'skipped'));
      const head = el('div', 'trace-head');
      head.appendChild(el('span', 'trace-state', trace.considered ? (trace.fired ? 'fired' : 'no') : 'skipped'));
      head.appendChild(el('code', 'trace-id', trace.ruleId));
      head.appendChild(el('span', 'trace-title', trace.title));
      head.appendChild(el('span', 'trace-reads', trace.reads.length + ' reads'));
      head.appendChild(el('code', 'trace-hash', trace.hash.slice(0, 10)));
      row.appendChild(head);

      if (trace.skipReason) {
        row.appendChild(el('div', 'trace-detail muted', trace.skipReason));
      } else {
        if (trace.guardSource) {
          const guard = el('div', 'trace-detail');
          guard.appendChild(el('code', 'guard', trace.guardSource));
          guard.appendChild(el('span', 'guard-result', ' -> ' + String(trace.guardResult)));
          row.appendChild(guard);
        }
        if (trace.reads.length) {
          const reads = el('div', 'trace-reads-list');
          trace.reads.forEach((read) => {
            const item = el('div', 'read' + (read.path.indexOf('[*]') !== -1 ? ' vertical' : ''));
            item.appendChild(el('code', 'read-path', read.path));
            item.appendChild(el('span', 'read-value',
              read.kind === 'collection' ? ('collection of ' + read.value) : JSON.stringify(read.value)));
            reads.appendChild(item);
          });
          row.appendChild(reads);
        }
        Object.keys(trace.sets).forEach((name) => {
          row.appendChild(el('div', 'trace-set', 'set derived.' + name + ' = ' + trace.sets[name]));
        });
      }
      box.appendChild(row);
    });
  }

  function showExplain(code) {
    const box = $('#eval-explain');
    box.innerHTML = '';
    if (!code || !lastDecision) {
      box.appendChild(el('div', 'muted', 'Run a scenario that produces a finding, then press "why".'));
      return;
    }
    const chain = E.explain(lastDecision, code);
    if (!chain.length) {
      box.appendChild(el('div', 'muted', 'No chain for ' + code));
      return;
    }
    box.appendChild(el('div', 'explain-title', 'why ' + code + ' was raised'));
    chain.forEach((trace, index) => {
      const step = el('div', 'explain-step');
      step.style.marginLeft = (index * 18) + 'px';
      const head = el('div', 'explain-head');
      head.appendChild(el('code', 'trace-id', trace.ruleId + ' v' + trace.version));
      head.appendChild(el('code', 'trace-hash', trace.hash.slice(0, 10)));
      head.appendChild(el('span', 'trace-title', trace.title));
      step.appendChild(head);
      if (trace.guardSource) {
        step.appendChild(el('div', 'explain-guard', trace.guardSource + '  ->  ' + String(trace.guardResult)));
      }
      trace.reads.forEach((read) => {
        step.appendChild(el('div', 'explain-read',
          read.path + ' = ' + (read.kind === 'collection' ? ('[' + read.value + ' items]') : JSON.stringify(read.value))));
      });
      Object.keys(trace.sets).forEach((name) => {
        step.appendChild(el('div', 'explain-set', 'set derived.' + name + ' = ' + trace.sets[name]));
      });
      box.appendChild(step);
    });
  }

  /* ================================================================== */
  /* Section: authoring                                                  */
  /* ================================================================== */

  const AUTHORING = {
    yaml: `- id: FTL-020
  version: "3"
  priority: 21
  title: Minimum rest before duty
  when: crew.rest_hours_before_duty < limits.min_rest_hours
  emit:
    code: REST_INSUFFICIENT
    severity: hard
    message: "Rest of {actual}h before report falls short of the {required}h minimum"
    detail:
      actual: crew.rest_hours_before_duty
      required: limits.min_rest_hours`,

    python: `@builder.rule("FTL-020", version="3", priority=21)
def minimum_rest(f):
    """Rest before report must meet the minimum."""
    if f.crew.rest_hours_before_duty < f.limits.min_rest_hours:
        emit("REST_INSUFFICIENT",
             severity="hard",
             message="Rest of {actual}h before report falls short of the "
                     "{required}h minimum",
             actual=f.crew.rest_hours_before_duty,
             required=f.limits.min_rest_hours)

# The function is never called. The decorator reads its source, parses it,
# and compiles it into the same Rule object the YAML loader produces.`,

    table: `id,version,priority,when crew.rest_hours_before_duty <,then code,then severity,then message
FTL-020,3,21,=limits.min_rest_hours,REST_INSUFFICIENT,hard,Rest before report falls short of the minimum

# Headers hold the fact path and the operator. Cells hold values.
# A cell beginning with "=" is an expression, and that escape hatch is
# visible in the table, which is the point.`
  };

  function showAuthoring(kind) {
    $('#authoring-code').textContent = AUTHORING[kind];
    $$('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.kind === kind));
  }

  /* ================================================================== */
  /* Section: shadow run                                                 */
  /* ================================================================== */

  const HARD_CODES = ['FTL_FDP_EXCEEDED', 'REST_INSUFFICIENT', 'BLOCK_HOURS_28D_EXCEEDED',
    'DUTY_HOURS_7D_EXCEEDED', 'CABIN_CREW_BELOW_MINIMUM', 'INEXPERIENCED_PILOT_PAIRING',
    'LINE_TRAINING_WITHOUT_LTC', 'NOT_TYPE_RATED', 'ETOPS_NOT_QUALIFIED', 'MEDICAL_EXPIRED'];

  function perturb(base, random) {
    const facts = JSON.parse(JSON.stringify(base));
    facts.duty.sectors = [1, 2, 2, 3, 4, 5, 6][Math.floor(random() * 7)];
    facts.duty.acclimatised = random() > 0.25;
    facts.duty.is_augmented = random() > 0.85;
    facts.duty.additional_crew = facts.duty.is_augmented ? Math.floor(random() * 3) : 0;

    const start = new Date('2026-08-14T05:00:00Z').getTime() + Math.floor(random() * 12 - 6) * 900000;
    const length = [7.5, 9, 10.5, 11, 12, 13, 14, 15.5][Math.floor(random() * 8)];
    facts.duty.start_utc = new Date(start).toISOString().replace('.000', '');
    facts.duty.end_utc = new Date(start + length * 3600000).toISOString().replace('.000', '');

    facts.crew.rest_hours_before_duty = Math.round((9 + random() * 11) * 10) / 10;
    facts.crew.hours_last_28d = Math.round((30 + random() * 69) * 10) / 10;
    facts.crew.duty_hours_last_7d = Math.round((15 + random() * 50) * 10) / 10;
    facts.crew.standby_hours_before_report = [0, 0, 0, 2, 4][Math.floor(random() * 5)];

    const departure = start + 3600000;
    const block = 1.5 + random() * 7.5;
    facts.flight.departure_utc = new Date(departure).toISOString().replace('.000', '');
    facts.flight.arrival_utc = new Date(departure + block * 3600000).toISOString().replace('.000', '');
    facts.flight.departure_date = facts.flight.departure_utc.slice(0, 10);

    facts.flight.roster.forEach((m) => {
      if (m.rank === 'CP' || m.rank === 'FO') {
        m.hours_on_type = [45, 70, 95, 120, 400, 900, 3200][Math.floor(random() * 7)];
      }
      if (random() > 0.93) m.is_under_line_training = true;
    });
    return facts;
  }

  /* The stand in for the incumbent. Three planted divergences, all of them
   * the kind of thing that survives for years in a long lived ruleset. */
  function legacyDecision(facts) {
    const codes = [];
    const hours = (Date.parse(facts.duty.end_utc) - Date.parse(facts.duty.start_utc)) / 3600000;
    const limits = facts.limits;

    // Divergence 1: a flat limit, no sector reduction, no unacclimatised penalty.
    let limit = limits.max_fdp_hours_base;
    if (facts.duty.is_augmented && facts.duty.additional_crew >= 1) {
      limit += facts.duty.additional_crew >= 2 ? 6 : 4;
    }
    if (hours > limit) codes.push('FTL_FDP_EXCEEDED');

    if (facts.crew.rest_hours_before_duty < limits.min_rest_hours) codes.push('REST_INSUFFICIENT');

    // Divergence 2: an off by one on the cumulative check.
    const block = (Date.parse(facts.flight.arrival_utc) - Date.parse(facts.flight.departure_utc)) / 3600000;
    if (facts.crew.hours_last_28d + block > limits.max_hours_28d - 1) codes.push('BLOCK_HOURS_28D_EXCEEDED');

    if (facts.crew.duty_hours_last_7d > limits.max_duty_7d) codes.push('DUTY_HOURS_7D_EXCEEDED');

    const cabin = facts.flight.roster.filter((m) => m.rank === 'SCC' || m.rank === 'CC').length;
    if (cabin < limits.min_cabin_crew) codes.push('CABIN_CREW_BELOW_MINIMUM');

    const green = facts.flight.roster.filter((m) =>
      (m.rank === 'CP' || m.rank === 'FO') && m.hours_on_type < 100).length;
    if (green > 1) codes.push('INEXPERIENCED_PILOT_PAIRING');

    // Divergence 3: LINE_TRAINING_WITHOUT_LTC was never implemented.

    const quals = facts.crew.qualifications || [];
    if (quals.indexOf(facts.flight.aircraft_type) === -1) codes.push('NOT_TYPE_RATED');
    if (facts.flight.is_etops && quals.indexOf('ETOPS') === -1) codes.push('ETOPS_NOT_QUALIFIED');
    if (String(facts.crew.medical_expiry) < String(facts.flight.departure_date)) codes.push('MEDICAL_EXPIRED');

    return codes.sort();
  }

  function runShadow() {
    const count = parseInt($('#shadow-count').value, 10) || 400;
    const random = E.mulberry32(20260801);
    const bases = Object.keys(D.SCENARIOS).map((k) => D.SCENARIOS[k].facts);
    const comparisons = [];

    for (let i = 0; i < count; i += 1) {
      const facts = perturb(bases[Math.floor(random() * bases.length)], random);
      const decision = E.evaluate(ruleset, facts, { client: 'AIRLINE_A', asOf: '2026-08-14' });
      const canonCodes = decision.findings
        .filter((f) => HARD_CODES.indexOf(f.code) !== -1)
        .map((f) => f.code).sort();
      const legacyCodes = legacyDecision(facts).filter((c) => HARD_CODES.indexOf(c) !== -1);

      const missing = legacyCodes.filter((c) => canonCodes.indexOf(c) === -1);
      const extra = canonCodes.filter((c) => legacyCodes.indexOf(c) === -1);
      let status = 'match';
      if (missing.length && extra.length) status = 'both_differ';
      else if (missing.length) status = 'missing_in_canon';
      else if (extra.length) status = 'extra_in_canon';

      comparisons.push({
        status, missing, extra,
        fired: decision.traces.filter((t) => t.fired).map((t) => t.ruleId),
        micros: decision.micros
      });
    }

    const matched = comparisons.filter((c) => c.status === 'match').length;
    const diverged = comparisons.filter((c) => c.status !== 'match');
    const agreed = comparisons.filter((c) => c.status === 'match');

    $('#shadow-total').textContent = comparisons.length;
    $('#shadow-agreement').textContent = ((matched / comparisons.length) * 100).toFixed(1) + '%';
    const latencies = comparisons.map((c) => c.micros).sort((a, b) => a - b);
    $('#shadow-latency').textContent = latencies[Math.floor(latencies.length / 2)].toFixed(0) + ' us';

    const statusBox = $('#shadow-status');
    statusBox.innerHTML = '';
    const counts = {};
    comparisons.forEach((c) => { counts[c.status] = (counts[c.status] || 0) + 1; });
    Object.keys(counts).sort().forEach((status) => {
      const row = el('div', 'stat-row');
      row.appendChild(el('span', 'stat-label', status.replace(/_/g, ' ')));
      row.appendChild(el('span', 'stat-value', String(counts[status])));
      statusBox.appendChild(row);
    });

    const codeCounts = {};
    diverged.forEach((c) => {
      c.missing.forEach((code) => {
        codeCounts[code] = codeCounts[code] || { missing: 0, extra: 0 };
        codeCounts[code].missing += 1;
      });
      c.extra.forEach((code) => {
        codeCounts[code] = codeCounts[code] || { missing: 0, extra: 0 };
        codeCounts[code].extra += 1;
      });
    });
    const codeBox = $('#shadow-codes');
    codeBox.innerHTML = '';
    Object.keys(codeCounts)
      .sort((a, b) => (codeCounts[b].missing + codeCounts[b].extra) - (codeCounts[a].missing + codeCounts[a].extra))
      .forEach((code) => {
        const row = el('div', 'stat-row');
        row.appendChild(el('code', 'stat-label', code));
        row.appendChild(el('span', 'stat-value',
          'canon missed ' + codeCounts[code].missing + ', canon added ' + codeCounts[code].extra));
        codeBox.appendChild(row);
      });

    const rules = new Set();
    comparisons.forEach((c) => c.fired.forEach((id) => rules.add(id)));
    const suspects = Array.from(rules).map((id) => {
      const inBad = diverged.filter((c) => c.fired.indexOf(id) !== -1).length;
      const inGood = agreed.filter((c) => c.fired.indexOf(id) !== -1).length;
      const badRate = diverged.length ? inBad / diverged.length : 0;
      const goodRate = agreed.length ? inGood / agreed.length : 0;
      const lift = goodRate ? badRate / goodRate : (badRate ? Infinity : 0);
      return { id, inBad, inGood, lift };
    }).filter((s) => s.inBad > 0);
    const rank = (x) => (x.lift === Infinity ? 1e9 : x.lift);
    suspects.sort((a, b) => (rank(b) - rank(a)) || (b.inBad - a.inBad));

    const suspectBox = $('#shadow-suspects');
    suspectBox.innerHTML = '';
    suspects.slice(0, 6).forEach((s) => {
      const row = el('div', 'suspect');
      row.appendChild(el('code', 'suspect-id', s.id));
      row.appendChild(el('span', 'suspect-detail',
        'fired in ' + s.inBad + ' divergent / ' + s.inGood + ' matching'));
      row.appendChild(el('span', 'suspect-lift',
        s.lift === Infinity ? 'only in divergent cases' : 'lift ' + s.lift.toFixed(2)));
      suspectBox.appendChild(row);
    });

    const canonAdded = diverged.filter((c) => c.extra.length).length;
    const canonMissed = diverged.filter((c) => c.missing.length).length;
    const named = suspects.slice(0, 3).map((s) => s.id);
    setPlain('#shadow-plain',
      'Out of ' + plural(comparisons.length, 'assignment') + ', the two systems gave '
      + 'the same answer ' + matched + ' times ('
      + ((matched / comparisons.length) * 100).toFixed(0) + '%). '
      + 'Canon flagged something the old system missed in '
      + plural(canonAdded, 'case') + ', and missed something the old system flagged in '
      + plural(canonMissed, 'case') + '. '
      + (named.length
        ? 'Almost all of the disagreement traces back to ' + named.join(', ')
          + '. That shortlist is the point: you are not reading the old code to find them.'
        : 'No single rule stands out.')
      + ' A disagreement is not automatically Canon being wrong. On real traffic this '
      + 'is usually where somebody discovers the old system has had a fault for years.');

    $('#shadow-results').classList.remove('hidden');
  }

  /* ================================================================== */
  /* Section: what-if replay                                             */
  /* ================================================================== */

  /* The proposal, mirroring examples/proposals/2026-09-fatigue-package.yaml.
   * Four edits: two that bite, one that changes the content hash and moves
   * nothing, and one new rule whose blast radius nobody has estimated. */
  function candidateRules() {
    return D.RULES.map((rule) => {
      if (rule.id === 'FTL-002') {
        return Object.assign({}, rule, {
          version: '4',
          sets: {
            max_fdp_hours: {
              src: 'max(9.0, limits.max_fdp_hours_base - 0.75 * (duty.sectors - 2))',
              fn: (f, $) => $.max(9.0, $.num(f.limits.max_fdp_hours_base)
                - 0.75 * ($.num(f.duty.sectors) - 2))
            }
          }
        });
      }
      if (rule.id === 'FTL-020') {
        return Object.assign({}, rule, {
          version: '4',
          whenSrc: 'crew.rest_hours_before_duty < limits.min_rest_hours + 0.5',
          when: (f, $) => $.lt(f.crew.rest_hours_before_duty,
            $.num(f.limits.min_rest_hours) + 0.5)
        });
      }
      if (rule.id === 'FTL-040') {
        // Priority orders findings in the output and never affects logic.
        return Object.assign({}, rule, { priority: 26 });
      }
      return rule;
    }).concat([{
      id: 'CREW-006', version: '1', priority: 35, clients: ['*'],
      title: 'Commander experience minimum',
      tags: ['composition', 'proposed'],
      vertical: true,
      whenSrc: "sum(1 for m in flight.roster if m.rank == 'CP' and m.hours_on_type < 500) > 0",
      when: (f, $) => $.gt($.count(f.flight.roster, (m) => $.all(
        () => $.eq(m.rank, 'CP'),
        () => $.lt(m.hours_on_type, 500)
      )), 0),
      emit: {
        code: 'COMMANDER_BELOW_EXPERIENCE_MINIMUM', severity: 'hard',
        message: 'The commander holds under the 500h on type minimum',
        detail: {}
      }
    }]);
  }

  function upstreamChanged(rs, ruleId, changed) {
    const found = new Set();
    const seen = new Set([ruleId]);
    const queue = [ruleId];
    while (queue.length) {
      const current = queue.pop();
      (rs.ruleDerived[current] || []).forEach((path) => {
        (rs.producers[path] || []).forEach((producer) => {
          if (seen.has(producer)) return;
          seen.add(producer);
          if (changed.has(producer)) found.add(producer);
          queue.push(producer);
        });
      });
    }
    return Array.from(found).sort();
  }

  function blame(decision, rs, code, direction, changed) {
    const trace = decision.traces.find((t) => t.emitted && t.emitted.code === code);
    if (!trace) return null;
    const ruleChanged = changed.has(trace.ruleId);
    return {
      code, direction,
      ruleId: trace.ruleId,
      ruleChanged,
      via: ruleChanged ? [] : upstreamChanged(rs, trace.ruleId, changed)
    };
  }

  function runWhatIf() {
    const count = parseInt($('#whatif-count').value, 10) || 400;
    const candidate = new E.RuleSet('crew_rostering', '2026.09.0-rc1',
      candidateRules(), D.DERIVED_POLICY);

    const baseHashes = {};
    ruleset.rules.forEach((r) => { baseHashes[r.id] = r.hash; });
    const changed = new Set();
    candidate.rules.forEach((r) => {
      if (baseHashes[r.id] !== r.hash) changed.add(r.id);
    });
    Object.keys(baseHashes).forEach((id) => {
      if (!candidate.byId[id]) changed.add(id);
    });

    const random = E.mulberry32(20260901);
    const bases = Object.keys(D.SCENARIOS).map((k) => D.SCENARIOS[k].facts);
    const flips = [];
    const firedEver = new Set();
    let unchanged = 0;

    for (let i = 0; i < count; i += 1) {
      const facts = perturb(bases[Math.floor(random() * bases.length)], random);
      const opts = { client: 'AIRLINE_A', asOf: '2026-08-14' };
      const before = E.evaluate(ruleset, facts, opts);
      const after = E.evaluate(candidate, facts, opts);
      before.traces.forEach((t) => { if (t.fired) firedEver.add(t.ruleId); });
      after.traces.forEach((t) => { if (t.fired) firedEver.add(t.ruleId); });

      const beforeCodes = before.findings.map((f) => f.code).sort();
      const afterCodes = after.findings.map((f) => f.code).sort();
      const added = afterCodes.filter((c) => beforeCodes.indexOf(c) === -1);
      const removed = beforeCodes.filter((c) => afterCodes.indexOf(c) === -1);

      const derivedMoved = {};
      Object.keys(Object.assign({}, before.derived, after.derived)).forEach((name) => {
        if (before.derived[name] !== after.derived[name]) {
          derivedMoved[name] = { before: before.derived[name], after: after.derived[name] };
        }
      });

      let kind = null;
      if (added.length || removed.length) {
        if (before.ok && !after.ok) kind = 'newly blocked';
        else if (!before.ok && after.ok) kind = 'newly allowed';
        else kind = 'findings changed';
      } else if (Object.keys(derivedMoved).length) {
        kind = 'derived only';
      }
      if (!kind) { unchanged += 1; continue; }

      const responsible = [];
      added.forEach((code) => {
        const entry = blame(after, candidate, code, 'added', changed);
        if (entry) responsible.push(entry);
      });
      removed.forEach((code) => {
        const entry = blame(before, ruleset, code, 'removed', changed);
        if (entry) responsible.push(entry);
      });
      flips.push({ kind, added, removed, responsible, derivedMoved });
    }

    // headline
    $('#whatif-total').textContent = count;
    $('#whatif-moved').textContent = flips.length;
    $('#whatif-rate').textContent = ((flips.length / count) * 100).toFixed(1) + '%';

    const kindBox = $('#whatif-kinds');
    kindBox.innerHTML = '';
    const kinds = {};
    flips.forEach((f) => { kinds[f.kind] = (kinds[f.kind] || 0) + 1; });
    const row = (parent, label, value, labelClass) => {
      const line = el('div', 'stat-row');
      line.appendChild(el(labelClass === 'code' ? 'code' : 'span', 'stat-label', label));
      line.appendChild(el('span', 'stat-value', String(value)));
      parent.appendChild(line);
    };
    row(kindBox, 'unchanged', unchanged);
    Object.keys(kinds).sort().forEach((k) => row(kindBox, k, kinds[k]));

    const codeBox = $('#whatif-codes');
    codeBox.innerHTML = '';
    const codes = {};
    flips.forEach((f) => {
      f.added.forEach((c) => {
        codes[c] = codes[c] || { up: 0, down: 0 };
        codes[c].up += 1;
      });
      f.removed.forEach((c) => {
        codes[c] = codes[c] || { up: 0, down: 0 };
        codes[c].down += 1;
      });
    });
    Object.keys(codes)
      .sort((a, b) => (codes[b].up + codes[b].down) - (codes[a].up + codes[a].down))
      .forEach((c) => row(codeBox, c,
        'newly raised ' + codes[c].up + ', withdrawn ' + codes[c].down, 'code'));
    if (!Object.keys(codes).length) codeBox.appendChild(el('div', 'muted', 'no findings moved'));

    const blameBox = $('#whatif-blame');
    blameBox.innerHTML = '';
    const attribution = {};
    const implicated = new Set();
    flips.forEach((f) => {
      f.responsible.forEach((entry) => {
        implicated.add(entry.ruleId);
        entry.via.forEach((v) => implicated.add(v));
        const key = entry.ruleId;
        attribution[key] = attribution[key] || { n: 0, changed: entry.ruleChanged, via: new Set() };
        attribution[key].n += 1;
        entry.via.forEach((v) => attribution[key].via.add(v));
      });
    });
    Object.keys(attribution)
      .sort((a, b) => attribution[b].n - attribution[a].n)
      .forEach((id) => {
        const info = attribution[id];
        const line = el('div', 'suspect');
        line.appendChild(el('code', 'suspect-id', id));
        line.appendChild(el('span', 'suspect-detail', info.n + ' decisions'));
        line.appendChild(el('span', 'suspect-lift', info.changed
          ? 'changed in this proposal'
          : (info.via.size
            ? 'unchanged, reached via ' + Array.from(info.via).join('/')
            : 'unchanged, cause not in the diff')));
        blameBox.appendChild(line);
      });
    if (!Object.keys(attribution).length) {
      blameBox.appendChild(el('div', 'muted', 'nothing to attribute'));
    }

    const inertBox = $('#whatif-inert');
    inertBox.innerHTML = '';
    const inert = Array.from(changed).filter((id) => !implicated.has(id)).sort();
    if (inert.length) {
      inert.forEach((id) => {
        const line = el('div', 'stat-row');
        line.appendChild(el('code', 'stat-label', id));
        line.appendChild(el('span', 'stat-value', 'hash changed, moved nothing'));
        inertBox.appendChild(line);
      });
    } else {
      inertBox.appendChild(el('div', 'muted', 'every changed rule moved at least one decision'));
    }

    const deadBox = $('#whatif-dead');
    deadBox.innerHTML = '';
    const allIds = new Set(Object.keys(baseHashes).concat(candidate.rules.map((r) => r.id)));
    const dead = Array.from(allIds).filter((id) => !firedEver.has(id)).sort();
    if (dead.length) {
      dead.forEach((id) => {
        const line = el('div', 'stat-row');
        line.appendChild(el('code', 'stat-label', id));
        line.appendChild(el('span', 'stat-value', 'never fired'));
        deadBox.appendChild(line);
      });
    } else {
      deadBox.appendChild(el('div', 'muted', 'every rule fired at least once'));
    }

    $('#whatif-changed').textContent = Array.from(changed).sort().join(', ');

    const blocked = flips.filter((f) => f.kind === 'newly blocked').length;
    const allowed = flips.filter((f) => f.kind === 'newly allowed').length;
    const topRule = Object.keys(attribution)
      .sort((a, b) => attribution[b].n - attribution[a].n)[0];
    let plain = 'If this change went live, ' + flips.length + ' of the '
      + plural(count, 'decision') + ' replayed would come out differently ('
      + ((flips.length / count) * 100).toFixed(0) + '%). ';
    if (blocked) {
      plain += plural(blocked, 'assignment') + ' that pass'
        + (blocked === 1 ? 'es' : '') + ' today would be blocked tomorrow. ';
    }
    if (allowed) {
      plain += plural(allowed, 'assignment') + ' blocked today would be allowed. ';
    }
    if (topRule) {
      const info = attribution[topRule];
      plain += 'Most of it comes back to ' + topRule + ', which '
        + (info.changed
          ? 'this proposal changed directly. '
          : 'this proposal did not touch: it relies on '
            + Array.from(info.via).join(' and ') + ', which did change. ');
    }
    plain += inert.length
      ? plural(inert.length, 'rule') + ' changed and moved nothing at all: '
        + inert.join(', ') + '. That is the part worth saying out loud in a review.'
      : 'Every rule this proposal touched moved at least one decision.';
    setPlain('#whatif-plain', plain);

    $('#whatif-results').classList.remove('hidden');
  }

  /* ================================================================== */
  /* Section: deploy ledger and tamper evidence                          */
  /* ================================================================== */

  const GENESIS = '0'.repeat(64);
  let ledger = [];

  function manifestFor(mutations, version, createdAt) {
    const entries = D.RULES.map((rule) => {
      const mutated = mutations[rule.id];
      const canonicalForm = Object.assign({}, JSON.parse(JSON.stringify({
        id: rule.id, version: rule.version, when: rule.whenSrc || null,
        emit: rule.emit ? { code: rule.emit.code, severity: rule.emit.severity } : null,
        priority: rule.priority
      })), mutated || {});
      return {
        rule_id: rule.id,
        version: canonicalForm.version,
        hash: E.contentHash(canonicalForm)
      };
    }).sort((a, b) => a.rule_id.localeCompare(b.rule_id));

    let layer = entries.map((e) => E.sha256('leaf:' + e.hash)).sort();
    while (layer.length > 1) {
      const next = [];
      for (let i = 0; i < layer.length; i += 2) {
        const left = layer[i];
        const right = i + 1 < layer.length ? layer[i + 1] : left;
        next.push(E.sha256('node:' + left + right));
      }
      layer = next;
    }
    return { ruleset_version: version, created_at: createdAt, entries, merkle_root: layer[0] };
  }

  function buildLedger() {
    const changed002 = { when: 'duty.sectors > 3' };
    const changed002q = { version: '3' };
    const releases = [
      { version: '2026.05.1', at: '2026-05-04T09:12:00Z', by: 'a.mensah', env: 'prod',
        mutations: {} },
      { version: '2026.06.1', at: '2026-06-11T14:03:00Z', by: 'r.patel', env: 'prod',
        mutations: { 'FTL-002': changed002 } },
      { version: '2026.07.1', at: '2026-07-02T08:41:00Z', by: 'r.patel', env: 'prod',
        mutations: { 'FTL-002': changed002, 'QUAL-002': changed002q } },
      { version: '2026.08.1', at: '2026-08-01T10:20:00Z', by: 'l.nakamura', env: 'prod',
        mutations: {
          'FTL-002': changed002,
          'QUAL-002': changed002q,
          'FTL-020': { when: 'crew.rest_hours_before_duty < limits.min_rest_hours - 1' }
        } }
    ];

    ledger = [];
    let prev = GENESIS;
    releases.forEach((release, index) => {
      const manifest = manifestFor(release.mutations, release.version, release.at);
      const record = {
        seq: index + 1,
        environment: release.env,
        deployed_at: release.at,
        deployed_by: release.by,
        prev_hash: prev,
        manifest
      };
      record.entry_hash = E.contentHash({
        seq: record.seq, environment: record.environment,
        deployed_at: record.deployed_at, deployed_by: record.deployed_by,
        prev_hash: record.prev_hash, manifest: record.manifest
      });
      prev = record.entry_hash;
      ledger.push(record);
    });
    renderLedger();
    renderBlame();
    verifyLedger();
  }

  function renderLedger() {
    const box = $('#ledger-list');
    box.innerHTML = '';
    ledger.forEach((record) => {
      const row = el('div', 'ledger-row');
      row.appendChild(el('span', 'ledger-seq', '#' + record.seq));
      row.appendChild(el('span', 'ledger-when', record.deployed_at.replace('T', ' ').replace('Z', '')));
      row.appendChild(el('span', 'ledger-env', record.environment));
      row.appendChild(el('span', 'ledger-version', 'v' + record.manifest.ruleset_version));
      row.appendChild(el('code', 'ledger-root', record.manifest.merkle_root.slice(0, 12)));
      row.appendChild(el('span', 'ledger-by', record.deployed_by));
      box.appendChild(row);
    });
  }

  function renderBlame() {
    const ruleId = $('#blame-rule').value;
    const box = $('#blame-result');
    box.innerHTML = '';
    let previous = null;
    const changes = [];
    ledger.forEach((record) => {
      const entry = record.manifest.entries.find((e) => e.rule_id === ruleId);
      if (!entry) return;
      if (entry.hash !== previous) {
        changes.push({
          seq: record.seq, at: record.deployed_at, by: record.deployed_by,
          change: previous === null ? 'added' : 'modified',
          from: previous ? previous.slice(0, 12) : null,
          to: entry.hash.slice(0, 12),
          version: record.manifest.ruleset_version
        });
        previous = entry.hash;
      }
    });
    if (!changes.length) {
      box.appendChild(el('div', 'muted', 'no record of this rule'));
      return;
    }
    changes.forEach((change) => {
      const row = el('div', 'blame-row');
      row.appendChild(el('span', 'blame-seq', '#' + change.seq));
      row.appendChild(el('span', 'blame-when', change.at.replace('T', ' ').replace('Z', '')));
      row.appendChild(el('span', 'blame-change ' + change.change, change.change));
      row.appendChild(el('code', 'blame-hash', (change.from || '-') + ' -> ' + change.to));
      row.appendChild(el('span', 'blame-by', change.by));
      box.appendChild(row);
    });
    const note = el('div', 'blame-note',
      changes.length === 1
        ? 'This rule has been unchanged since it was first deployed.'
        : 'Changed in ' + (changes.length - 1) + ' deployment(s) after the first.');
    box.appendChild(note);
  }

  function verifyLedger() {
    const problems = [];
    let expected = GENESIS;
    ledger.forEach((record, index) => {
      if (record.seq !== index + 1) problems.push('entry ' + (index + 1) + ': sequence number is ' + record.seq);
      if (record.prev_hash !== expected) {
        problems.push('entry ' + record.seq + ': prev_hash does not match the previous entry, '
          + 'so an entry has been inserted, removed or reordered');
      }
      const recomputed = E.contentHash({
        seq: record.seq, environment: record.environment,
        deployed_at: record.deployed_at, deployed_by: record.deployed_by,
        prev_hash: record.prev_hash, manifest: record.manifest
      });
      if (recomputed !== record.entry_hash) {
        problems.push('entry ' + record.seq + ': contents were edited after the fact (stored '
          + record.entry_hash.slice(0, 12) + ', recomputed ' + recomputed.slice(0, 12) + ')');
      }
      expected = record.entry_hash;
    });

    const box = $('#ledger-verify');
    box.innerHTML = '';
    box.className = 'verify ' + (problems.length ? 'bad' : 'good');
    if (!problems.length) {
      box.appendChild(el('div', 'verify-line', 'Chain intact. ' + ledger.length + ' entries verified.'));
      box.appendChild(el('div', 'verify-head', 'head ' + ledger[ledger.length - 1].entry_hash.slice(0, 24)));
    } else {
      box.appendChild(el('div', 'verify-line', 'LEDGER FAILED VERIFICATION'));
      problems.forEach((problem) => box.appendChild(el('div', 'verify-problem', problem)));
    }
  }

  /* ================================================================== */
  /* Boot                                                                */
  /* ================================================================== */

  /* ================================================================== */
  /* Reading mode                                                        */
  /* ================================================================== */

  function setMode(mode) {
    document.body.className = 'mode-' + mode;
    $$('#mode-switch button').forEach((button) => {
      button.classList.toggle('active', button.dataset.mode === mode);
    });
  }

  function boot() {
    $$('#mode-switch button').forEach((button) => {
      button.addEventListener('click', () => setMode(button.dataset.mode));
    });
    setMode('plain');

    // header numbers
    $('#stat-rules').textContent = D.RULES.length;
    $('#stat-strata').textContent = ruleset.strata.length;
    $('#stat-fields').textContent = ruleset.allPaths.length;
    $('#stat-root').textContent = ruleset.merkleRoot(null, null).slice(0, 12);

    $('#strata-map').innerHTML = '';
    ruleset.strata.forEach((stratum, index) => {
      const group = el('div', 'stratum');
      group.appendChild(el('div', 'stratum-label', 'round ' + (index + 1)));
      const chips = el('div', 'stratum-chips');
      stratum.forEach((rule) => {
        const chip = el('code', 'stratum-chip', rule.id);
        chip.title = rule.title;
        chips.appendChild(chip);
      });
      group.appendChild(chips);
      $('#strata-map').appendChild(group);
    });

    buildRuleToggles();
    ['#contract-client', '#contract-date', '#contract-scenario'].forEach((selector) => {
      $(selector).addEventListener('change', updateContract);
    });
    updateContract();

    ['#eval-scenario', '#eval-client', '#eval-date'].forEach((selector) => {
      $(selector).addEventListener('change', runScenario);
    });
    runScenario();

    $$('.tab').forEach((tab) => {
      tab.addEventListener('click', () => showAuthoring(tab.dataset.kind));
    });
    showAuthoring('yaml');

    $('#shadow-run').addEventListener('click', runShadow);
    $('#whatif-run').addEventListener('click', runWhatIf);

    const blameSelect = $('#blame-rule');
    D.RULES.forEach((rule) => {
      const option = document.createElement('option');
      option.value = rule.id;
      option.textContent = rule.id + '  ' + rule.title;
      blameSelect.appendChild(option);
    });
    blameSelect.value = 'FTL-002';
    blameSelect.addEventListener('change', renderBlame);

    $('#ledger-rebuild').addEventListener('click', buildLedger);
    $('#ledger-tamper').addEventListener('click', () => {
      if (ledger.length > 1) {
        ledger[1].deployed_by = 'someone.else';
        ledger[1].manifest.ruleset_version = '2026.06.9';
      }
      renderLedger();
      verifyLedger();
    });
    buildLedger();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}());
