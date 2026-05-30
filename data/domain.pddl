(define (domain scenario_solver_example1-domain)
 (:requirements :strips :typing :equality :numeric-fluents)
 (:types trackpart arrivaltrain trainunit)
 (:predicates 
             (free ?trackpart - trackpart)
             (at ?unit - arrivaltrain ?trackpart - trackpart)
             (parking_allowed ?trackpart - trackpart)
             (parked ?train - arrivaltrain)
             (connected ?from_ - trackpart ?to - trackpart)
 )
 (:functions 
             (arrival ?train - arrivaltrain)
             (entry_distance ?trackpart - trackpart)
             (departure_rank ?train - arrivaltrain)
 )
 (:action move
  :parameters ( ?t - arrivaltrain ?l_from - trackpart ?l_to - trackpart)
  :precondition (and (at ?t ?l_from) (connected ?l_from ?l_to))
  :effect (and (at ?t ?l_to) (not (at ?t ?l_from))))
 (:action park
  :parameters ( ?t - arrivaltrain ?l - trackpart)
  :precondition (and (at ?t ?l) (parking_allowed ?l) (= (departure_rank ?t) (entry_distance ?l)))
  :effect (and (parked ?t)))
)
