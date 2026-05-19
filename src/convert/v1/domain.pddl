(define (domain scenario_solver_2600tw_instance7-domain)
 (:requirements :strips :typing)
 (:types trackpart arrivaltrain trainunit)
 (:predicates 
             (free ?trackpart - trackpart)
             (at ?unit - arrivaltrain ?trackpart - trackpart)
 )
 (:functions 
             (arrival ?train - arrivaltrain)
 )
 (:action move
  :parameters ( ?t - arrivaltrain ?l_from - trackpart ?l_to - trackpart)
  :precondition (and (at ?t ?l_from))
  :effect (and (at ?t ?l_to) (not (at ?t ?l_from))))
)
