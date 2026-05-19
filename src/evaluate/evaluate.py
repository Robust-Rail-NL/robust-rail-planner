import os
import sys
import subprocess

scenarios_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "generate", "scenarios"))
location_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "generate", "location.json"))
convert_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "convert"))
path_to_planner = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plan", "planner.jl"))
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

convert_variants = [d for d in os.listdir(convert_dir) if os.path.isdir(os.path.join(convert_dir, d))]

for variant_name in convert_variants:
    variant_dir = os.path.join(convert_dir, variant_name)
    domain_path = os.path.abspath(os.path.join(variant_dir, "domain.pddl"))

    converter_files = [f for f in os.listdir(variant_dir) if f.startswith("convert_") and f.endswith(".py")]
    if not converter_files:
        print(f"No converter found in {variant_dir}, skipping...")
        continue
    path_to_converter = os.path.join(variant_dir, converter_files[0])
    domain_written = False

    for n_trains in os.listdir(scenarios_dir):
        for order_name in os.listdir(os.path.join(scenarios_dir, n_trains)):
            solver_dir = os.path.join(scenarios_dir, n_trains, order_name, "solver")
            if not os.path.isdir(solver_dir):
                continue

            pddl_dir = os.path.join(scenarios_dir, n_trains, order_name, "pddl", variant_name)
            os.makedirs(pddl_dir, exist_ok=True)

            for file in os.listdir(solver_dir):
                if file.startswith("scenario_solver_") and file.endswith(".json"):
                    solver_path = os.path.join(solver_dir, file)
                    pddl_path = os.path.abspath(os.path.join(pddl_dir, file.replace(".json", ".pddl")))

                    print(f"[{variant_name}] Converting {file}...")
                    if not domain_written:
                        subprocess.run([sys.executable, path_to_converter,
                            "-s", solver_path,
                            "-l", location_file,
                            "-o", pddl_path,
                            "-d", domain_path,
                        ])
                        domain_written = True
                    else:
                        subprocess.run([sys.executable, path_to_converter,
                            "-s", solver_path,
                            "-l", location_file,
                            "-o", pddl_path,
                        ])

                    print(f"[{variant_name}] Planning {file}...")
                    subprocess.run(["julia", f"--project={project_root}",
                        path_to_planner, pddl_path, domain_path,
                    ])

                    