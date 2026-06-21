import os
import importlib.util

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # planning-approach
GENERATE_DIR = os.path.join(os.path.dirname(BASE_DIR), "scenario-planning-inputs", "Location_KleineBinckhorst")

# SCENARIO SETTINGS
number_trains = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35]
number_instances = 10
matching = {0: "FIFO", 1: "random", 2: "LIFO"}
default_seed = 42
time_window_per_train = [10000000000000000]
mixed_traffic = False
min_gap_on_gateway = 180
perform_servicing = True

def generate():
    spec = importlib.util.spec_from_file_location("generate", os.path.join(GENERATE_DIR, "generate.py"))
    generate_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generate_module)
    generate_module.generate_scenarios(
        number_trains=number_trains, number_instances=number_instances, matching=matching,
        default_seed=default_seed, time_window_per_train=time_window_per_train,
        mixed_traffic=mixed_traffic, min_gap_on_gateway=min_gap_on_gateway,
        perform_servicing=perform_servicing,
    )