from pyomo.environ import (
    ConcreteModel,
    Set,
    Param,
    Var,
    Constraint,
    Objective,
    Reals,  # type: ignore
    NonNegativeReals,  # type: ignore
)
from pyomo.opt import SolverFactory
import pandas as pd
from datetime import timedelta
import os
import sys
import re
import argparse
import json
import pickle
import plotly.express as px

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.data_utils import (
    infer_frequency,
    create_directory,
)
import numpy as np
import wandb

from utils.paths import ROOT_DIR, EVAL_DIR, RESULTS_DIR

from utils.model_utils import Config


def run_opt(load_forecast, bss_energy, peak, config, prices=None):
    ################################
    # MPC optimization model
    ################################
    m = ConcreteModel()
    m.T = Set(initialize=list(range(len(load_forecast))))

    # MPC Params
    # m.energy_prices = Param(m.T, initialize=prices)
    m.demand = Param(m.T, initialize=load_forecast)
    m.bss_energy_start = Param(initialize=bss_energy)
    m.monthly_peak = Param(initialize=peak)

    # Config Params
    m.bss_size = Param(initialize=config.bat_size_kwh)
    m.bss_eff = Param(initialize=config.bat_efficiency)
    m.bss_max_pow = Param(initialize=config.bat_max_power)
    m.bss_end_soc = Param(initialize=config.bat_end_soc_weight)
    m.peak_cost = Param(initialize=config.peak_cost)

    # variables
    m.net_load = Var(m.T, domain=Reals)
    m.peak = Var(domain=NonNegativeReals)
    m.dev_peak_plus = Var(domain=NonNegativeReals)
    m.dev_peak_minus = Var(domain=NonNegativeReals)
    m.bss_p_ch = Var(m.T, domain=Reals)
    m.bss_en = Var(m.T, domain=NonNegativeReals)

    def energy_balance(m, t):
        return m.net_load[t] == m.bss_eff * m.bss_p_ch[t] + m.demand[t]

    m.energy_balance = Constraint(m.T, rule=energy_balance)

    def operation_peak(m, t):
        return m.peak >= m.net_load[t]

    m.operation_peak = Constraint(m.T, rule=operation_peak)

    def relevant_peak(m):
        return m.peak - m.monthly_peak == m.dev_peak_plus - m.dev_peak_minus

    m.relevant_peak = Constraint(rule=relevant_peak)

    def bat_soc(m, t):
        if t == 0:
            return m.bss_en[t] == m.bss_energy_start + m.bss_p_ch[t]
        else:
            return m.bss_en[t] == m.bss_en[t - 1] + m.bss_p_ch[t]

    m.bat_soc = Constraint(m.T, rule=bat_soc)

    def bat_lim_energy(m, t):
        return m.bss_en[t] <= m.bss_size

    m.bat_lim_energy = Constraint(m.T, rule=bat_lim_energy)

    def bat_lim_power_pos(m, t):
        return m.bss_p_ch[t] <= m.bss_max_pow

    m.bat_lim_power_pos = Constraint(m.T, rule=bat_lim_power_pos)

    def bat_lim_power_neg(m, t):
        return -m.bss_max_pow <= m.bss_p_ch[t]

    m.bat_lim_power_neg = Constraint(m.T, rule=bat_lim_power_neg)

    def cost(m):
        terminal_cost_weight = m.bss_end_soc
        final_soc = m.bss_en[len(m.T) - 1]
        terminal_cost = terminal_cost_weight * (final_soc - m.bss_energy_start) ** 2

        above_peak_costs = (m.dev_peak_plus + m.monthly_peak) * m.peak_cost
        below_peak_reward = (m.dev_peak_minus + m.monthly_peak) * m.peak_cost

        peak_costs = above_peak_costs + below_peak_reward

        return peak_costs + terminal_cost

    m.ObjectiveFunction = Objective(rule=cost)

    opt = SolverFactory("cplex")
    results = opt.solve(m)

    # get results
    res_df = pd.DataFrame(index=list(m.T))
    for v in [m.net_load, m.bss_p_ch, m.bss_en]:
        res_df = res_df.join(pd.Series(v.get_values(), name=v.getname()))

    sp = res_df.iloc[1].to_dict()

    return sp


def run_operations(dfs_mpc, config):
    print("Running operations")
    # function to run operations of given a forecast and system data

    # initializing peak (it will store the historical peak)
    peak = config.peak_init
    # initialize energy in the battery with the initial soc
    energy_in_the_battery = config.bat_size_kwh * config.bat_initial_soc

    operations = {}
    for t, df_mpc in enumerate(dfs_mpc[:-1]):
        load_forecast = df_mpc.iloc[:, 0].values.tolist()
        # prices = df_mpc.iloc[:, 2].values.tolist()

        set_point = run_opt(
            load_forecast=load_forecast,
            bss_energy=energy_in_the_battery,
            peak=peak,
            config=config,
        )

        set_point_time = t + 1  # set point is applied to the next hour
        load_set_point_time = (
            dfs_mpc[set_point_time].iloc[[0], [1]].values.flatten()[0]
        )  # get the ground truth
        net_load = load_set_point_time + set_point["bss_p_ch"]

        if net_load > peak:
            peak = min(
                net_load, max(dfs_mpc[set_point_time].iloc[:, 0].values.tolist())
            )

        set_point.update(
            {
                "load": load_set_point_time,
                "opr_net_load": net_load,
                "peak": peak,
                "forecast": load_forecast[0],
            }
        )

        # update energy in the battery
        energy_in_the_battery = set_point["bss_en"]

        operations.update({set_point_time: set_point})

    return pd.DataFrame(operations).T


def calculate_operational_costs(df_operations, config):
    # ex-post cost calculation

    print("Calculating costs")

    cost_results = {}

    bss_en = df_operations.filter(like="bss_en")

    peak_penalty = df_operations["opr_net_load"].max() * config.peak_cost

    cost_results.update({"total_cost": peak_penalty})

    return cost_results


def run_nle(eval_dict, scale, location, horizon, season, model):
    print(f"Running NLE for Horizon: {horizon} and Season: {season}")

    # creating directory for results
    MPC_RESULTS_DIR = os.path.join(RESULTS_DIR, "mpc_results", scale, location)
    create_directory(MPC_RESULTS_DIR)

    # loading the nle_config
    with open(os.path.join(ROOT_DIR, "nle_config.json"), "r") as fp:
        nle_config = json.load(fp)
        nle_config = Config.from_dict(nle_config, is_initial_config=False)

    # getting predictions for the given horizon and season
    ts_list_per_model = eval_dict[horizon][season][
        0
    ]  # idx=0: ts_list (see eval_utils.py)

    # getting the ground truth
    gt = eval_dict[horizon][season][
        2
    ].pd_dataframe()  # idx=2: groundtruth (see eval_utils.py)
    gt.columns = ["gt_" + gt.columns[0]]  # rename column to avoid confusion

    # grabbing stats for scaling the predictions and ground truth -> energy prices are a function of the ground truth
    gt_max = gt.max().values[0]
    gt_min = gt.min().values[0]

    print(f"Running MPC for {model}")

    # Ground truth operations and costs as baseline
    dfs_mpc_gt = [
        (
            (
                (
                    ts_forecast.pd_dataframe()
                    .join(gt, how="left")
                    .join(
                        gt.rename({gt.columns[0]: "gt_sup"}, axis=1), how="left"
                    )  # adding gt twice so when we use .iloc[:, 1:] we get the gt, gt, ep
                )
                - gt_min
            )
            / (gt_max - gt_min)
        ).iloc[:, 1:]
        for ts_forecast in ts_list_per_model[model]
    ]

    df_operations_gt = run_operations(dfs_mpc_gt, nle_config)
    costs_gt = calculate_operational_costs(df_operations_gt, nle_config)

    # Model operations and costs
    dfs_mpc = [
        ((ts_forecast.pd_dataframe().join(gt, how="left")) - gt_min) / (gt_max - gt_min)
        for ts_forecast in ts_list_per_model[model]
    ]

    # calculating the operations and costs for the ground truth

    df_operations = run_operations(dfs_mpc, nle_config)
    costs = calculate_operational_costs(df_operations, nle_config)
    nle_score = costs["total_cost"] - costs_gt["total_cost"]

    df_operations_gt.columns = ["gt_" + col for col in df_operations_gt.columns]
    df_operations = pd.concat([df_operations, df_operations_gt], axis=1)
    df_operations.to_csv(MPC_RESULTS_DIR + f"/{model}_operations.csv")

    return nle_score


def main():
    parser = argparse.ArgumentParser(description="Run MPC")
    parser.add_argument("--scale", type=str, help="Spatial scale", default="1_county")
    parser.add_argument("--location", type=str, help="Location", default="Los_Angeles")
    parser.add_argument("--season", type=str, help="Winter or Summer", default="Summer")
    parser.add_argument("--horizon", type=int, help="MPC horizon", default=24)
    parser.add_argument("--model", type=str, default="RandomForest")
    args = parser.parse_args()

    with open(os.path.join(EVAL_DIR, f"{args.scale}/{args.location}.pkl"), "rb") as f:
        eval_dict = pickle.load(f)

    costs = run_nle(
        eval_dict, args.scale, args.location, args.horizon, args.season, args.model
    )

    print(f"NLE score: {costs}")


if __name__ == "__main__":
    main()
