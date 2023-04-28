import argparse
import json
import os

import numpy as np
import pandas as pd
from sklearn.model_selection import RepeatedStratifiedKFold

parser = argparse.ArgumentParser()
parser.add_argument("--data_dir", type=str)
parser.add_argument(
    "--config_path",
    type=str,
)


def main(data_dir, config_path) -> int:
    with open(f"{config_path}/config.json") as f:
        config = json.load(f)
    np.random.seed(config["random_state"])
    splits_path = os.path.join(data_dir, "splits", "TCGA")
    os.makedirs(splits_path, exist_ok=True)
    for cancer in config["datasets"]:
        print(cancer)
        data_path = f"processed/TCGA/{cancer}_data_preprocessed.csv"
        data = pd.read_csv(
            os.path.join(data_dir, data_path),
            low_memory=False,
        )

        # Exact column choice doesn't matter
        # as this is only to create the splits anyway.
        X = data[[i for i in data.columns if i not in ["OS_days", "OS"]]]
        cv = RepeatedStratifiedKFold(
            n_repeats=config["n_outer_repetitions"],
            n_splits=config["n_outer_splits"],
            random_state=config["random_state"],
        )
        splits = [i for i in cv.split(X, data["OS"])]
        pd.DataFrame([i[0] for i in splits]).to_csv(
            f"{splits_path}/{cancer}_train_splits.csv",
            index=False,
        )
        pd.DataFrame([i[1] for i in splits]).to_csv(
            f"{splits_path}/{cancer}_test_splits.csv",
            index=False,
        )
    return 0


if __name__ == "__main__":
    args = parser.parse_args()
    main(
        args.data_dir,
        args.config_path,
    )
