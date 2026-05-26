import os
import json
import shutil
import subprocess

# number_trains = [5, 10, 15, 20, 25, 30, 31, 32, 33, 34, 35]
number_trains = [1]
number_instances = 10
matching = {0: "FIFO", 1: "random", 2: "LIFO"}
default_seed = 42
time_window_per_train = [520]
mixed_traffic = False
min_gap_on_gateway = 180

# Not implemented yet
perform_servicing = False

config_file_name = os.path.join(os.path.dirname(__file__), "configurations", "generation_config.json")
path_to_generator = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "robust-rail-generator", "src", "main.py")

for (j, n) in enumerate(number_trains):
    for (m, order_name) in matching.items():
        base_dir = os.path.join(os.path.dirname(__file__), "scenarios", f"{n}trains", order_name)
        tors_dir = os.path.join(base_dir, "tors")
        solver_dir = os.path.join(base_dir, "solver")
        pddl_dir = os.path.join(base_dir, "pddl")
        
        for d in [tors_dir, solver_dir, pddl_dir]:
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d)

        for i in range(number_instances):
            seed = default_seed * (j + i + 1)
            for t in time_window_per_train:
                end_time = n * t
                config = {
                    "location": "KleineBinckhorst",
                    "number_of_trains": n,
                    "start_time": 0,
                    "end_time": end_time,
                    "seed": seed,
                    "use_default_material": True,
                    "trains_given": False,
                    "perform_servicing": perform_servicing,
                    "mixed_traffic": mixed_traffic,
                    "matching": m,
                    "min_gap_on_gateway": min_gap_on_gateway,
                    "gateway": {
                        "arrival": [15],
                        "departure": [15],
                    },
                    "train_unit_distribution": {
                        "train_unit_types": ["VIRM-4", "VIRM-6", "ICM-3", "ICM-4", "SNG-3", "SNG-4", "SLT-4", "SLT-6"],
                        "super_type_ratio": 0.5,
                        "units_per_composition": [1, 2, 3],
                        "matching_complexity": 0.3,
                        "instanding_ratio": 0.3,
                        "outstanding_ratio": 0.1
                    }
                }
                json.dump(config, open(config_file_name, "w"), indent=4)
                current_file = os.path.dirname(__file__)

                scenario_name = f"scenario_{end_time}TW{'_mixed' if mixed_traffic else ''}{'_servicing' if perform_servicing else ''}_instance{i}.json"

                tors_path = os.path.join(tors_dir, scenario_name)
                solver_name = scenario_name.replace("scenario_", "scenario_solver_")
                solver_path = os.path.join(solver_dir, solver_name)

                # Create instance
                subprocess.run(['python3', path_to_generator,
                    "-p", os.path.dirname(__file__),
                    "-c", config_file_name,
                    "-s", tors_path,
                ])

                # Generator puts solver file next to TORS file, so move it to solver_dir
                generated_solver = os.path.join(tors_dir, solver_name)
                if os.path.exists(generated_solver):
                    shutil.move(generated_solver, solver_path)