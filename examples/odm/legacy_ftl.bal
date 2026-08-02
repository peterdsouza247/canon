// Illustrative IBM ODM business action language export.
//
// Six rules. Four convert cleanly, two do not, and the two that do not are the
// interesting ones: an ODM collection quantifier and a condition that mixes
// 'and' with 'or' without brackets. The importer refuses both rather than
// guessing, because guessing at precedence in a legality rule is how you end up
// rostering somebody illegally and not finding out for six months.

rule FTL_010_MaxDutyPeriod {
	property priority = 20
	property status = "present"
	when {
		the duty hours of 'the duty' is more than 13 ;
	} then {
		add error "Flight duty period exceeds the permitted maximum" to 'the result' ;
	}
}

rule FTL_020_MinimumRest {
	property priority = 21
	when {
		the rest before duty of 'the crew member' is less than 12 ;
	} then {
		add error "Rest before report is below the minimum" to 'the result' ;
	}
}

rule FTL_030_BlockHours28 {
	property priority = 22
	when {
		the hours last 28d of 'the crew member' is at least 100 ;
	} then {
		add error "Block hours in the last 28 days exceed the limit" to 'the result' ;
	}
}

rule QUAL_002_Etops {
	property priority = 51
	when {
		the is etops of 'the flight' is true ;
		and the qualifications of 'the crew member' does not contain "ETOPS" ;
	} then {
		add error "ETOPS sector requires an ETOPS qualified crew member" to 'the result' ;
	}
}

rule FTL_045_MixedPrecedence {
	property priority = 26
	when {
		the sectors of 'the duty' is more than 4 ;
		and the acclimatised of 'the duty' is false ;
		or the encroaches wocl of 'the duty' is true ;
	} then {
		add warning "Duty pattern needs review" to 'the result' ;
	}
}

rule CREW_002_InexperiencedPairing {
	property priority = 31
	when {
		there is at least 2 crew member in 'the roster' such that the hours on type of this crew member is less than 100 ;
	} then {
		add error "Two inexperienced pilots are paired on this flight" to 'the result' ;
	}
}
