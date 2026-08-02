/* The demo ruleset and fact documents.
 *
 * These mirror examples/rules/ftl.yaml, examples/rules/qualifications.csv and
 * examples/data/scenarios.json. Each rule carries both the executable form and
 * the YAML source it corresponds to, so the page can show you the rule you are
 * running rather than an approximation of it.
 *
 * Everything below is invented. The operator code ZQ is fictional, as are the
 * flight numbers, aircraft registrations, crew identifiers and personal
 * details. The duty and rest limits are shaped like published flight time
 * limitation regulation but are not a copy of any operator's rules.
 */

'use strict';

const RULES = [

  /* ---------------- stratum 0: derive the applicable limits -------------- */

  {
    id: 'FTL-001', version: '4', priority: 10, clients: ['*'],
    title: 'Baseline flight duty period',
    tags: ['ftl', 'derivation'],
    note: 'The unrestricted limit. Everything else reduces it.',
    sets: {
      max_fdp_hours: {
        src: 'limits.max_fdp_hours_base',
        fn: (f, $) => $.v(f.limits.max_fdp_hours_base)
      }
    },
    yaml: `- id: FTL-001
  version: "4"
  title: Baseline flight duty period
  priority: 10
  set:
    max_fdp_hours: limits.max_fdp_hours_base`
  },

  {
    id: 'FTL-002', version: '3', priority: 11, clients: ['*'],
    title: 'Sector reduction',
    tags: ['ftl', 'derivation'],
    note: 'Half an hour off for each sector past the second, floored at nine.',
    whenSrc: 'duty.sectors > 2',
    when: (f, $) => $.gt(f.duty.sectors, 2),
    sets: {
      max_fdp_hours: {
        src: 'max(9.0, limits.max_fdp_hours_base - 0.5 * (duty.sectors - 2))',
        fn: (f, $) => $.max(9.0, $.num(f.limits.max_fdp_hours_base) - 0.5 * ($.num(f.duty.sectors) - 2))
      }
    },
    yaml: `- id: FTL-002
  version: "3"
  title: Sector reduction
  priority: 11
  when: duty.sectors > 2
  set:
    max_fdp_hours: >
      max(9.0, limits.max_fdp_hours_base - 0.5 * (duty.sectors - 2))`
  },

  {
    id: 'FTL-003', version: '2', priority: 12, clients: ['*'],
    title: 'Unacclimatised reduction',
    tags: ['ftl', 'derivation'],
    note: 'Two hours off if the crew member has not acclimatised to local time.',
    whenSrc: 'duty.acclimatised == False',
    when: (f, $) => $.isFalse(f.duty.acclimatised),
    sets: {
      max_fdp_hours: {
        src: 'limits.max_fdp_hours_base - 2.0',
        fn: (f, $) => $.num(f.limits.max_fdp_hours_base) - 2.0
      }
    },
    yaml: `- id: FTL-003
  version: "2"
  title: Unacclimatised reduction
  priority: 12
  when: duty.acclimatised == False
  set:
    max_fdp_hours: limits.max_fdp_hours_base - 2.0`
  },

  {
    id: 'FTL-004', version: '1', priority: 13, clients: ['*'],
    title: 'No augmentation by default',
    tags: ['ftl', 'derivation'],
    note: 'A floor, so the consuming rule never reasons about a missing value.',
    sets: {
      augmentation_credit: { src: '0.0', fn: () => 0.0 }
    },
    yaml: `- id: FTL-004
  version: "1"
  title: No augmentation by default
  priority: 13
  set:
    augmentation_credit: "0.0"`
  },

  {
    id: 'FTL-005', version: '2', priority: 14, clients: ['*'],
    title: 'Augmented crew extension',
    tags: ['ftl', 'derivation'],
    note: 'Augmentation extends rather than restricts, so its policy is max.',
    whenSrc: 'duty.is_augmented == True and duty.additional_crew >= 1',
    when: (f, $) => $.all(
      () => $.isTrue(f.duty.is_augmented),
      () => $.gte(f.duty.additional_crew, 1)
    ),
    sets: {
      augmentation_credit: {
        src: '6.0 if duty.additional_crew >= 2 else 4.0',
        fn: (f, $) => ($.num(f.duty.additional_crew) >= 2 ? 6.0 : 4.0)
      }
    },
    yaml: `- id: FTL-005
  version: "2"
  title: Augmented crew extension
  priority: 14
  when: duty.is_augmented == True and duty.additional_crew >= 1
  set:
    augmentation_credit: "6.0 if duty.additional_crew >= 2 else 4.0"`
  },

  /* ---------------- stratum 1: test against the derived limits ----------- */

  {
    id: 'FTL-010', version: '5', priority: 20, clients: ['*'],
    title: 'Flight duty period within the permitted maximum',
    tags: ['ftl', 'legality'],
    reads: ['derived.max_fdp_hours', 'derived.augmentation_credit'],
    note: 'The headline check. Its dependency on the five derivation rules is declared, not implied by ordering.',
    whenSrc: 'hours_between(duty.start_utc, duty.end_utc) > derived.max_fdp_hours + derived.augmentation_credit',
    when: (f, $) => $.gt(
      $.hours(f.duty.start_utc, f.duty.end_utc),
      $.num(f.derived.max_fdp_hours) + $.num(f.derived.augmentation_credit)
    ),
    emit: {
      code: 'FTL_FDP_EXCEEDED', severity: 'hard',
      message: 'Flight duty period of {actual_hours}h exceeds the permitted {limit_hours}h for this duty',
      detail: {
        actual_hours: {
          src: 'round(hours_between(duty.start_utc, duty.end_utc), 2)',
          fn: (f, $) => $.round($.hours(f.duty.start_utc, f.duty.end_utc), 2)
        },
        limit_hours: {
          src: 'round(derived.max_fdp_hours + derived.augmentation_credit, 2)',
          fn: (f, $) => $.round($.num(f.derived.max_fdp_hours) + $.num(f.derived.augmentation_credit), 2)
        },
        sectors: { src: 'duty.sectors', fn: (f, $) => $.v(f.duty.sectors) }
      }
    },
    yaml: `- id: FTL-010
  version: "5"
  title: Flight duty period within the permitted maximum
  priority: 20
  reads:
    - derived.max_fdp_hours
    - derived.augmentation_credit
  when: >
    hours_between(duty.start_utc, duty.end_utc)
    > derived.max_fdp_hours + derived.augmentation_credit
  emit:
    code: FTL_FDP_EXCEEDED
    severity: hard
    message: >
      Flight duty period of {actual_hours}h exceeds the permitted
      {limit_hours}h for this duty
    detail:
      actual_hours: round(hours_between(duty.start_utc, duty.end_utc), 2)
      limit_hours: round(derived.max_fdp_hours + derived.augmentation_credit, 2)
      sectors: duty.sectors`
  },

  {
    id: 'FTL-020', version: '3', priority: 21, clients: ['*'],
    title: 'Minimum rest before duty',
    tags: ['ftl', 'rest'],
    whenSrc: 'crew.rest_hours_before_duty < limits.min_rest_hours',
    when: (f, $) => $.lt(f.crew.rest_hours_before_duty, f.limits.min_rest_hours),
    emit: {
      code: 'REST_INSUFFICIENT', severity: 'hard',
      message: 'Rest of {actual}h before report falls short of the {required}h minimum',
      detail: {
        actual: { src: 'crew.rest_hours_before_duty', fn: (f, $) => $.v(f.crew.rest_hours_before_duty) },
        required: { src: 'limits.min_rest_hours', fn: (f, $) => $.v(f.limits.min_rest_hours) }
      }
    },
    yaml: `- id: FTL-020
  version: "3"
  title: Minimum rest before duty
  priority: 21
  when: crew.rest_hours_before_duty < limits.min_rest_hours
  emit:
    code: REST_INSUFFICIENT
    severity: hard
    message: "Rest of {actual}h before report falls short of the {required}h minimum"
    detail:
      actual: crew.rest_hours_before_duty
      required: limits.min_rest_hours`
  },

  {
    id: 'FTL-030', version: '2', priority: 22, clients: ['*'],
    title: 'Block hours in the preceding 28 days',
    tags: ['ftl', 'cumulative'],
    whenSrc: 'crew.hours_last_28d + hours_between(flight.departure_utc, flight.arrival_utc) > limits.max_hours_28d',
    when: (f, $) => $.gt(
      $.num(f.crew.hours_last_28d) + $.hours(f.flight.departure_utc, f.flight.arrival_utc),
      f.limits.max_hours_28d
    ),
    emit: {
      code: 'BLOCK_HOURS_28D_EXCEEDED', severity: 'hard',
      message: 'Assignment would take 28 day block hours to {projected}h against a {limit}h limit',
      detail: {
        projected: {
          src: 'round(crew.hours_last_28d + hours_between(flight.departure_utc, flight.arrival_utc), 1)',
          fn: (f, $) => $.round($.num(f.crew.hours_last_28d) + $.hours(f.flight.departure_utc, f.flight.arrival_utc), 1)
        },
        limit: { src: 'limits.max_hours_28d', fn: (f, $) => $.v(f.limits.max_hours_28d) }
      }
    },
    yaml: `- id: FTL-030
  version: "2"
  title: Block hours in the preceding 28 days
  priority: 22
  when: >
    crew.hours_last_28d
    + hours_between(flight.departure_utc, flight.arrival_utc)
    > limits.max_hours_28d
  emit:
    code: BLOCK_HOURS_28D_EXCEEDED
    severity: hard
    message: "Assignment would take 28 day block hours to {projected}h against a {limit}h limit"`
  },

  {
    id: 'FTL-040', version: '1', priority: 24, clients: ['*'],
    title: 'Cumulative duty in the preceding seven days',
    tags: ['ftl', 'cumulative'],
    whenSrc: 'crew.duty_hours_last_7d > limits.max_duty_7d',
    when: (f, $) => $.gt(f.crew.duty_hours_last_7d, f.limits.max_duty_7d),
    emit: {
      code: 'DUTY_HOURS_7D_EXCEEDED', severity: 'hard',
      message: 'Duty hours of {actual}h in the last seven days exceed the {limit}h limit',
      detail: {
        actual: { src: 'crew.duty_hours_last_7d', fn: (f, $) => $.v(f.crew.duty_hours_last_7d) },
        limit: { src: 'limits.max_duty_7d', fn: (f, $) => $.v(f.limits.max_duty_7d) }
      }
    },
    yaml: `- id: FTL-040
  version: "1"
  priority: 24
  when: crew.duty_hours_last_7d > limits.max_duty_7d
  emit:
    code: DUTY_HOURS_7D_EXCEEDED
    severity: hard`
  },

  {
    id: 'FTL-050', version: '1', priority: 40, clients: ['*'],
    title: 'Standby ahead of duty counts toward the duty period',
    tags: ['ftl', 'advisory'],
    reads: ['derived.max_fdp_hours'],
    note: 'Advisory rather than blocking. Planners want to know before the roster is published.',
    whenSrc: 'crew.standby_hours_before_report > 0 and hours_between(...) + standby * 0.25 > derived.max_fdp_hours',
    when: (f, $) => $.all(
      () => $.gt(f.crew.standby_hours_before_report, 0),
      () => $.gt(
        $.hours(f.duty.start_utc, f.duty.end_utc) + $.num(f.crew.standby_hours_before_report) * 0.25,
        f.derived.max_fdp_hours
      )
    ),
    emit: {
      code: 'STANDBY_PUSHES_FDP_OVER_LIMIT', severity: 'soft',
      message: 'With {standby}h of standby counted at a quarter rate, this duty would pass the {limit}h limit',
      detail: {
        standby: { src: 'crew.standby_hours_before_report', fn: (f, $) => $.v(f.crew.standby_hours_before_report) },
        limit: { src: 'derived.max_fdp_hours', fn: (f, $) => $.v(f.derived.max_fdp_hours) }
      }
    },
    yaml: `- id: FTL-050
  version: "1"
  priority: 40
  reads: [derived.max_fdp_hours]
  when: >
    crew.standby_hours_before_report > 0
    and hours_between(duty.start_utc, duty.end_utc)
        + crew.standby_hours_before_report * 0.25
        > derived.max_fdp_hours
  emit:
    code: STANDBY_PUSHES_FDP_OVER_LIMIT
    severity: soft`
  },

  /* ---------------- crew composition: the vertical slice ---------------- */

  {
    id: 'CREW-001', version: '3', priority: 30, clients: ['*'],
    title: 'Minimum cabin crew complement',
    tags: ['composition', 'vertical'],
    vertical: true,
    note: 'Needs every crew member on the flight, not the one in the request.',
    whenSrc: "sum(1 for m in flight.roster if m.rank in ['SCC', 'CC']) < limits.min_cabin_crew",
    when: (f, $) => $.lt(
      $.count(f.flight.roster, (m) => $.oneOf(m.rank, ['SCC', 'CC'])),
      f.limits.min_cabin_crew
    ),
    emit: {
      code: 'CABIN_CREW_BELOW_MINIMUM', severity: 'hard',
      message: '{actual} cabin crew rostered against a minimum of {required}',
      detail: {
        actual: {
          src: "sum(1 for m in flight.roster if m.rank in ['SCC', 'CC'])",
          fn: (f, $) => $.count(f.flight.roster, (m) => $.oneOf(m.rank, ['SCC', 'CC']))
        },
        required: { src: 'limits.min_cabin_crew', fn: (f, $) => $.v(f.limits.min_cabin_crew) }
      }
    },
    yaml: `- id: CREW-001
  version: "3"
  title: Minimum cabin crew complement
  priority: 30
  when: >
    sum(1 for m in flight.roster if m.rank in ['SCC', 'CC'])
    < limits.min_cabin_crew`
  },

  {
    id: 'CREW-002', version: '2', priority: 31, clients: ['*'],
    title: 'Inexperienced pilot pairing',
    tags: ['composition', 'vertical'],
    vertical: true,
    note: 'The rule that is impossible to express cleanly when the payload only carries the crew member named in the request.',
    whenSrc: "sum(1 for m in flight.roster if m.rank in ['CP','FO'] and m.hours_on_type < 100) > 1",
    when: (f, $) => $.gt(
      $.count(f.flight.roster, (m) => $.all(
        () => $.oneOf(m.rank, ['CP', 'FO']),
        () => $.lt(m.hours_on_type, 100)
      )), 1
    ),
    emit: {
      code: 'INEXPERIENCED_PILOT_PAIRING', severity: 'hard',
      message: '{count} pilots on this flight have under 100 hours on type',
      detail: {
        count: {
          src: "sum(1 for m in flight.roster if m.rank in ['CP','FO'] and m.hours_on_type < 100)",
          fn: (f, $) => $.count(f.flight.roster, (m) => $.all(
            () => $.oneOf(m.rank, ['CP', 'FO']),
            () => $.lt(m.hours_on_type, 100)
          ))
        }
      }
    },
    yaml: `- id: CREW-002
  version: "2"
  title: Inexperienced pilot pairing
  priority: 31
  when: >
    sum(1 for m in flight.roster
        if m.rank in ['CP', 'FO'] and m.hours_on_type < 100) > 1`
  },

  {
    id: 'CREW-003', version: '2', priority: 32, clients: ['*'],
    title: 'Line training needs a line training captain',
    tags: ['composition', 'vertical', 'training'],
    vertical: true,
    whenSrc: 'any(m.is_under_line_training for m in flight.roster) and not any(m.is_line_training_captain for m in flight.roster)',
    when: (f, $) => $.all(
      () => $.some(f.flight.roster, (m) => $.isTrue(m.is_under_line_training)),
      () => $.not($.some(f.flight.roster, (m) => $.isTrue(m.is_line_training_captain)))
    ),
    emit: {
      code: 'LINE_TRAINING_WITHOUT_LTC', severity: 'hard',
      message: 'A crew member is under line training with no line training captain on board',
      detail: {}
    },
    yaml: `- id: CREW-003
  version: "2"
  title: Line training needs a line training captain
  priority: 32
  when: >
    any(m.is_under_line_training for m in flight.roster)
    and not any(m.is_line_training_captain for m in flight.roster)`
  },

  {
    id: 'CREW-004', version: '1', priority: 33, clients: ['*'],
    title: 'Language capability for the destination',
    tags: ['composition', 'vertical', 'advisory'],
    vertical: true,
    whenSrc: 'flight.requires_local_language == True and not any(flight.local_language in m.languages for m in flight.roster)',
    when: (f, $) => $.all(
      () => $.isTrue(f.flight.requires_local_language),
      () => $.not($.some(f.flight.roster, (m) => $.has(m.languages, f.flight.local_language)))
    ),
    emit: {
      code: 'NO_LOCAL_LANGUAGE_SPEAKER', severity: 'soft',
      message: 'No rostered crew member speaks {language}',
      detail: {
        language: { src: 'flight.local_language', fn: (f, $) => $.v(f.flight.local_language) }
      }
    },
    yaml: `- id: CREW-004
  version: "1"
  priority: 33
  when: >
    flight.requires_local_language == True
    and not any(flight.local_language in m.languages for m in flight.roster)`
  },

  /* ---------------- qualifications, authored as a decision table -------- */

  {
    id: 'QUAL-001', version: '3', priority: 50, clients: ['*'],
    title: 'Type rating for the aircraft',
    tags: ['qualification', 'table'],
    authoring: 'table',
    whenSrc: 'flight.aircraft_type not in crew.qualifications',
    when: (f, $) => $.lacks(f.crew.qualifications, f.flight.aircraft_type),
    emit: {
      code: 'NOT_TYPE_RATED', severity: 'hard',
      message: 'Crew member does not hold a type rating for {required}',
      detail: {
        required: { src: 'flight.aircraft_type', fn: (f, $) => $.v(f.flight.aircraft_type) }
      }
    },
    yaml: `id,when crew.qualifications not contains,then code,then severity
QUAL-001,=flight.aircraft_type,NOT_TYPE_RATED,hard`
  },

  {
    id: 'QUAL-002', version: '2', priority: 51, clients: ['*'],
    title: 'ETOPS qualification',
    tags: ['qualification', 'table'],
    authoring: 'table',
    whenSrc: "flight.is_etops == True and 'ETOPS' not in crew.qualifications",
    when: (f, $) => $.all(
      () => $.isTrue(f.flight.is_etops),
      () => $.lacks(f.crew.qualifications, 'ETOPS')
    ),
    emit: {
      code: 'ETOPS_NOT_QUALIFIED', severity: 'hard',
      message: 'ETOPS sector requires an ETOPS qualified crew member',
      detail: {}
    },
    yaml: `id,when flight.is_etops ==,when crew.qualifications not contains,then code
QUAL-002,true,ETOPS,ETOPS_NOT_QUALIFIED`
  },

  {
    id: 'QUAL-005', version: '4', priority: 54, clients: ['*'],
    title: 'Class one medical certificate',
    tags: ['qualification', 'table'],
    authoring: 'table',
    whenSrc: 'crew.medical_expiry < flight.departure_date',
    when: (f, $) => (
      $.ctx.static
        ? ($.v(f.crew.medical_expiry), $.v(f.flight.departure_date), false)
        : String($.v(f.crew.medical_expiry)) < String($.v(f.flight.departure_date))
    ),
    emit: {
      code: 'MEDICAL_EXPIRED', severity: 'hard',
      message: 'Medical certificate expires on {expiry}, before the flight departs',
      detail: {
        expiry: { src: 'crew.medical_expiry', fn: (f, $) => $.v(f.crew.medical_expiry) }
      }
    },
    yaml: `id,when crew.medical_expiry <,then code,then severity
QUAL-005,=flight.departure_date,MEDICAL_EXPIRED,hard`
  },

  /* ---------------- tenant scoped and date effective -------------------- */

  {
    id: 'FTL-090', version: '1', priority: 25, clients: ['AIRLINE_B'],
    title: 'Additional rest margin for AIRLINE_B',
    tags: ['ftl', 'tenant'],
    note: 'Scoped to one operator, so its fields never appear in another operator’s payload contract.',
    whenSrc: 'crew.rest_hours_before_duty < limits.min_rest_hours + 2',
    when: (f, $) => $.lt(f.crew.rest_hours_before_duty, $.num(f.limits.min_rest_hours) + 2),
    emit: {
      code: 'REST_BELOW_OPERATOR_MARGIN', severity: 'soft',
      message: 'Rest of {actual}h in grade {grade} accommodation is inside the operator’s two hour margin',
      detail: {
        actual: { src: 'crew.rest_hours_before_duty', fn: (f, $) => $.v(f.crew.rest_hours_before_duty) },
        grade: { src: 'crew.hotel_rest_grade', fn: (f, $) => $.v(f.crew.hotel_rest_grade) }
      }
    },
    yaml: `- id: FTL-090
  version: "1"
  clients: [AIRLINE_B]
  priority: 25
  when: crew.rest_hours_before_duty < limits.min_rest_hours + 2
  emit:
    code: REST_BELOW_OPERATOR_MARGIN
    severity: soft
    detail:
      actual: crew.rest_hours_before_duty
      grade: crew.hotel_rest_grade`
  },

  {
    id: 'FTL-095', version: '1', priority: 15, clients: ['*'],
    effectiveFrom: '2026-09-01',
    title: 'Night duty reduction, from September 2026',
    tags: ['ftl', 'derivation', 'future'],
    note: 'Loaded now, dormant until its effective date. Nobody has to remember to deploy anything on the day.',
    whenSrc: 'duty.encroaches_wocl == True',
    when: (f, $) => $.isTrue(f.duty.encroaches_wocl),
    sets: {
      max_fdp_hours: {
        src: 'limits.max_fdp_hours_base - 1.5',
        fn: (f, $) => $.num(f.limits.max_fdp_hours_base) - 1.5
      }
    },
    yaml: `- id: FTL-095
  version: "1"
  priority: 15
  effective_from: "2026-09-01"
  when: duty.encroaches_wocl == True
  set:
    max_fdp_hours: limits.max_fdp_hours_base - 1.5`
  }
];

const DERIVED_POLICY = {
  max_fdp_hours: 'min',
  augmentation_credit: 'max'
};

/* ---------------------------------------------------------------------- */
/* Fact documents                                                          */
/* ---------------------------------------------------------------------- */

const LIMITS = {
  max_fdp_hours_base: 13.0,
  max_hours_28d: 100.0,
  max_hours_365d: 900.0,
  max_duty_7d: 60.0,
  min_rest_hours: 12.0,
  min_cabin_crew: 4
};

function member(id, rank, hours, extra) {
  return Object.assign({
    id: id,
    rank: rank,
    hours_on_type: hours,
    is_under_line_training: false,
    is_line_training_captain: false,
    languages: ['en'],
    base: 'LHR',
    seniority_years: 6,
    passport_expiry: '2031-04-01',
    contract_type: 'FULL_TIME'
  }, extra || {});
}

const SCENARIOS = {
  legal: {
    label: 'Clean assignment',
    blurb: 'Everything within limits. Watch how few of the planned fields are actually read.',
    client: 'AIRLINE_A',
    asOf: '2026-08-14',
    facts: {
      limits: LIMITS,
      crew: {
        id: 'C1001', rank: 'CP', base: 'LHR', seniority_years: 14,
        qualifications: ['A320', 'ETOPS', 'LVO', 'CAT_C', 'LINE_CHECK_CURRENT'],
        medical_expiry: '2027-03-31', hours_on_type: 4200,
        hours_last_28d: 62.0, hours_last_365d: 610.0, duty_hours_last_7d: 38.0,
        rest_hours_before_duty: 14.0, standby_hours_before_report: 0.0,
        hotel_rest_grade: 'A', home_address: '14 Elmfield Road, Hounslow',
        next_of_kin: 'A. Okonjo', payroll_number: 'P-88213',
        passport_number: 'GB4471029', bank_sort_code: '20-45-11',
        uniform_size: 'M', last_appraisal: '2026-02-11'
      },
      duty: {
        start_utc: '2026-08-14T05:15:00Z', end_utc: '2026-08-14T15:45:00Z',
        sectors: 2, acclimatised: true, is_augmented: false,
        additional_crew: 0, encroaches_wocl: false,
        report_location: 'LHR-T5-CREW', transport_booked: true
      },
      flight: {
        number: 'ZQ4101', aircraft_type: 'A320', registration: 'G-ZQAA',
        departure_utc: '2026-08-14T06:15:00Z', arrival_utc: '2026-08-14T09:05:00Z',
        departure_date: '2026-08-14', is_etops: false, is_lowvis_expected: false,
        destination_category: 'A', requires_local_language: false,
        local_language: null, passengers_booked: 168, cargo_tonnes: 2.4,
        gate: 'A12', slot_time: '2026-08-14T06:05:00Z',
        roster: [
          member('C1001', 'CP', 4200, { is_line_training_captain: true }),
          member('C1002', 'FO', 900, { languages: ['en', 'fr'] }),
          member('C2001', 'SCC', 2100, { languages: ['en', 'es'] }),
          member('C2002', 'CC', 800),
          member('C2003', 'CC', 640, { languages: ['en', 'it'] }),
          member('C2004', 'CC', 310)
        ]
      }
    }
  },

  fdp_breach: {
    label: 'Long unacclimatised duty, five sectors',
    blurb: 'Three derivation rules compete for the limit. The most restrictive wins, and the trace shows which.',
    client: 'AIRLINE_A',
    asOf: '2026-08-14',
    facts: {
      limits: LIMITS,
      crew: {
        id: 'C1001', rank: 'CP', base: 'LHR', seniority_years: 14,
        qualifications: ['A320', 'ETOPS', 'LVO', 'CAT_C', 'LINE_CHECK_CURRENT'],
        medical_expiry: '2027-03-31', hours_on_type: 4200,
        hours_last_28d: 88.0, hours_last_365d: 610.0, duty_hours_last_7d: 47.0,
        rest_hours_before_duty: 12.5, standby_hours_before_report: 4.0,
        hotel_rest_grade: 'B', home_address: '14 Elmfield Road, Hounslow',
        next_of_kin: 'A. Okonjo', payroll_number: 'P-88213',
        passport_number: 'GB4471029', bank_sort_code: '20-45-11',
        uniform_size: 'M', last_appraisal: '2026-02-11'
      },
      duty: {
        start_utc: '2026-08-14T05:00:00Z', end_utc: '2026-08-14T19:30:00Z',
        sectors: 5, acclimatised: false, is_augmented: false,
        additional_crew: 0, encroaches_wocl: false,
        report_location: 'LHR-T5-CREW', transport_booked: true
      },
      flight: {
        number: 'ZQ4207', aircraft_type: 'A320', registration: 'G-ZQAB',
        departure_utc: '2026-08-14T06:00:00Z', arrival_utc: '2026-08-14T18:40:00Z',
        departure_date: '2026-08-14', is_etops: false, is_lowvis_expected: false,
        destination_category: 'A', requires_local_language: false,
        local_language: null, passengers_booked: 174, cargo_tonnes: 1.9,
        gate: 'B33', slot_time: '2026-08-14T05:50:00Z',
        roster: [
          member('C1001', 'CP', 4200, { is_line_training_captain: true }),
          member('C1044', 'FO', 62),
          member('C1051', 'FO', 81),
          member('C2001', 'SCC', 2100),
          member('C2002', 'CC', 800),
          member('C2003', 'CC', 640),
          member('C2004', 'CC', 310)
        ]
      }
    }
  },

  composition: {
    label: 'The whole roster is the problem',
    blurb: 'Three findings that no amount of data about the crew member in the request could produce.',
    client: 'AIRLINE_A',
    asOf: '2026-08-14',
    facts: {
      limits: LIMITS,
      crew: {
        id: 'C1005', rank: 'CP', base: 'LHR', seniority_years: 9,
        qualifications: ['A320', 'CAT_C', 'LINE_CHECK_CURRENT'],
        medical_expiry: '2027-01-31', hours_on_type: 380,
        hours_last_28d: 51.0, hours_last_365d: 520.0, duty_hours_last_7d: 30.0,
        rest_hours_before_duty: 13.0, standby_hours_before_report: 0.0,
        hotel_rest_grade: 'B', home_address: '9 Bramley Court, Staines',
        next_of_kin: 'M. Ferreira', payroll_number: 'P-90114',
        passport_number: 'GB5510223', bank_sort_code: '30-11-08',
        uniform_size: 'L', last_appraisal: '2026-04-02'
      },
      duty: {
        start_utc: '2026-08-14T04:30:00Z', end_utc: '2026-08-14T13:00:00Z',
        sectors: 2, acclimatised: true, is_augmented: false,
        additional_crew: 0, encroaches_wocl: false,
        report_location: 'LHR-T5-CREW', transport_booked: true
      },
      flight: {
        number: 'ZQ4462', aircraft_type: 'A320', registration: 'G-ZQAC',
        departure_utc: '2026-08-14T05:30:00Z', arrival_utc: '2026-08-14T09:15:00Z',
        departure_date: '2026-08-14', is_etops: false, is_lowvis_expected: false,
        destination_category: 'C', requires_local_language: true,
        local_language: 'el', passengers_booked: 151, cargo_tonnes: 0.8,
        gate: 'C41', slot_time: '2026-08-14T05:20:00Z',
        roster: [
          member('C1005', 'CP', 380),
          member('C1090', 'FO', 55, { is_under_line_training: true }),
          member('C2020', 'SCC', 1500),
          member('C2021', 'CC', 400),
          member('C2022', 'CC', 220, { languages: ['en', 'fr'] })
        ]
      }
    }
  },

  qualifications: {
    label: 'ETOPS sector, lapsed paperwork',
    blurb: 'Decision table rules doing what decision tables are good at.',
    client: 'AIRLINE_A',
    asOf: '2026-08-14',
    facts: {
      limits: LIMITS,
      crew: {
        id: 'C1077', rank: 'FO', base: 'LHR', seniority_years: 3,
        qualifications: ['A320', 'LVO'],
        medical_expiry: '2026-08-10', hours_on_type: 1450,
        hours_last_28d: 44.0, hours_last_365d: 480.0, duty_hours_last_7d: 21.0,
        rest_hours_before_duty: 16.0, standby_hours_before_report: 0.0,
        hotel_rest_grade: 'A', home_address: '2 Kingsway, Feltham',
        next_of_kin: 'R. Nair', payroll_number: 'P-91882',
        passport_number: 'GB6620144', bank_sort_code: '40-22-19',
        uniform_size: 'S', last_appraisal: '2026-05-30'
      },
      duty: {
        start_utc: '2026-08-14T09:00:00Z', end_utc: '2026-08-14T19:00:00Z',
        sectors: 1, acclimatised: true, is_augmented: true,
        additional_crew: 1, encroaches_wocl: false,
        report_location: 'LHR-T5-CREW', transport_booked: true
      },
      flight: {
        number: 'ZQ4318', aircraft_type: 'B787', registration: 'G-ZQAD',
        departure_utc: '2026-08-14T10:00:00Z', arrival_utc: '2026-08-14T18:20:00Z',
        departure_date: '2026-08-14', is_etops: true, is_lowvis_expected: false,
        destination_category: 'B', requires_local_language: false,
        local_language: null, passengers_booked: 214, cargo_tonnes: 9.1,
        gate: 'A7', slot_time: '2026-08-14T09:45:00Z',
        roster: [
          member('C1003', 'CP', 6100, { is_line_training_captain: true }),
          member('C1077', 'FO', 1450),
          member('C1080', 'FO', 2200),
          member('C2010', 'SCC', 3000),
          member('C2011', 'CC', 1200),
          member('C2012', 'CC', 900),
          member('C2013', 'CC', 700),
          member('C2014', 'CC', 450)
        ]
      }
    }
  }
};

window.CanonData = { RULES, DERIVED_POLICY, SCENARIOS, LIMITS, member };
