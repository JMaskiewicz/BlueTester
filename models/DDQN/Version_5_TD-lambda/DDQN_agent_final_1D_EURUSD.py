"""
DDQN 5.1

- added TD lambda learning
- added alternative rewards


# TODO add saving the model

"""
import numpy as np
import pandas as pd
import time
import torch
import torch.nn as nn
import torch.optim as optim
from tqdm import tqdm
import random
from itertools import cycle
from concurrent.futures import ThreadPoolExecutor
import torch.nn.functional as F
from numba import jit
from torch.optim.lr_scheduler import ExponentialLR

from data.function.load_data import load_data_parallel
from data.function.rolling_window import rolling_window_datasets
from technical_analysys.add_indicators import add_indicators, add_returns, add_log_returns, add_time_sine_cosine
from functions.utilis import save_model
import backtest.backtest_functions.functions as BF
from functions.utilis import prepare_backtest_results, generate_index_labels, get_time

# import environment class
from trading_environment.environment import Trading_Environment_Basic

# import benchmark agents
from backtest.benchmark_agents import Buy_and_hold_Agent, Sell_and_hold_Agent

# Set seeds for reproducibility
torch.manual_seed(0)
np.random.seed(0)
random.seed(0)

"""
Reward Calculation function is the most crucial part of the RL algorithm. It is the function that determines the reward the agent receives for its actions.
currently there is 
"""

@jit(nopython=True)
def reward_calculation(previous_close, current_close, previous_position, current_position, leverage, provision):
    # Calculate the normal return
    if previous_close != 0:
        normal_return = (current_close - previous_close) / previous_close
    else:
        normal_return = 0

    # Calculate the base reward
    reward = normal_return * current_position * 1000

    # Penalize the agent for taking the wrong action
    if reward < 0:
        reward *= 1  # penalty for wrong action

    # Calculate the cost of provision if the position has changed, and it's not neutral (0).
    if current_position != previous_position and abs(current_position) == 1:
        provision_cost = - provision * 1000  # penalty for changing position
    elif current_position == previous_position and abs(current_position) == 1:
        provision_cost = + provision * 0
    else:
        provision_cost = 0

    # Apply the provision cost
    reward += provision_cost

    # Scale the reward to enhance its significance for the learning process
    final_reward = reward

    return final_reward

class DuelingQNetwork(nn.Module):
    def __init__(self, input_dims, n_actions, dropout_rate=1/8):
        super(DuelingQNetwork, self).__init__()
        self.fc1 = nn.Linear(input_dims, 512)
        self.dropout1 = nn.Dropout(dropout_rate)
        self.fc2 = nn.Linear(512, 512)
        self.dropout2 = nn.Dropout(dropout_rate)
        self.fc_value = nn.Linear(512, 256)
        self.value = nn.Linear(256, 1)
        self.fc_advantage = nn.Linear(512, 256)
        self.advantage = nn.Linear(256, n_actions)

    def forward(self, state):
        x = F.relu(self.fc1(state))
        x = self.dropout1(x)
        x = F.relu(self.fc2(x))
        x = self.dropout2(x)

        val = F.relu(self.fc_value(x))
        val = self.value(val)

        adv = F.relu(self.fc_advantage(x))
        adv = self.advantage(adv)

        # Combine value and advantage streams
        q_values = val + (adv - adv.mean(dim=1, keepdim=True))

        return q_values

class DQNMemory:
    def __init__(self, max_size, input_shape, n_actions):
        self.mem_size = max_size
        self.mem_cntr = 0
        self.state_memory = np.zeros((self.mem_size, *input_shape), dtype=np.float32)
        self.new_state_memory = np.zeros((self.mem_size, *input_shape), dtype=np.float32)
        self.action_memory = np.zeros(self.mem_size, dtype=np.int64)
        self.reward_memory = np.zeros(self.mem_size, dtype=np.float32)
        self.terminal_memory = np.zeros(self.mem_size, dtype=np.bool_)

    def store_transition(self, state, action, reward, state_, done):
        index = self.mem_cntr % self.mem_size
        self.state_memory[index] = state
        self.new_state_memory[index] = state_
        self.action_memory[index] = action
        self.reward_memory[index] = reward
        self.terminal_memory[index] = done

        self.mem_cntr += 1

    def sample_buffer(self, batch_size):
        max_mem = min(self.mem_cntr, self.mem_size)
        batch = np.random.choice(max_mem, batch_size, replace=False)

        states = self.state_memory[batch]
        actions = self.action_memory[batch]
        rewards = self.reward_memory[batch]
        states_ = self.new_state_memory[batch]
        dones = self.terminal_memory[batch]

        return states, actions, rewards, states_, dones

    def clear_memory(self):
        self.mem_cntr = 0
        self.state_memory = np.zeros_like(self.state_memory)
        self.new_state_memory = np.zeros_like(self.new_state_memory)
        self.action_memory = np.zeros_like(self.action_memory)
        self.reward_memory = np.zeros_like(self.reward_memory)
        self.terminal_memory = np.zeros_like(self.terminal_memory)

class DDQN_Agent:
    def __init__(self, input_dims, n_actions, n_epochs=1, mini_batch_size=256, gamma=0.99, policy_alpha=0.001,
                 target_alpha=0.0005, epsilon=1.0, epsilon_dec=1e-5, epsilon_end=0.01, mem_size=100000,
                 batch_size=64, replace=1000, weight_decay=0.0005, l1_lambda=1e-5, static_input_dims=1,
                 lr_decay_rate=0.999, premium_gamma=0.5, lambda_=0.75):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.n_actions = n_actions
        # self.device = torch.device("cpu")
        print(f"Using device: {self.device}")
        self.n_epochs = n_epochs  # Number of epochs
        self.mini_batch_size = mini_batch_size  # Mini-batch size
        self.gamma = gamma  # Discount factor
        self.epsilon = epsilon   # Exploration rate
        self.epsilon_dec = epsilon_dec  # Exploration rate decay
        self.epsilon_min = epsilon_end  # Minimum exploration rate
        self.mem_size = mem_size  # Memory size
        self.batch_size = batch_size  # Batch size
        self.replace_target_cnt = replace  # Replace target network count
        self.learn_step_counter = 0  # Learn step counter
        self.weight_decay = weight_decay  # Weight decay
        self.l1_lambda = l1_lambda  # L1 regularization lambda
        self.lr_decay_rate = lr_decay_rate  # Learning rate decay rate
        self.premium_gamma = premium_gamma  # Discount factor for the alternative rewards
        self.action_space = [i for i in range(n_actions)]  # Action space
        self.lambda_ = lambda_  # Lambda for TD(lambda) learning

        # Memory
        self.memory = DQNMemory(mem_size, (input_dims,), n_actions)

        # Policy and target networks
        self.q_policy = DuelingQNetwork(input_dims, n_actions).to(self.device)
        self.q_target = DuelingQNetwork(input_dims, n_actions).to(self.device)
        self.q_target.load_state_dict(self.q_policy.state_dict())
        self.q_target.eval()  # Set the target network to evaluation mode

        self.policy_lr = policy_alpha  # Policy learning rate
        self.target_lr = target_alpha  # Target learning rate
        self.policy_optimizer = optim.Adam(self.q_policy.parameters(), lr=self.policy_lr, weight_decay=weight_decay)
        self.target_optimizer = optim.Adam(self.q_target.parameters(), lr=self.target_lr, weight_decay=weight_decay)

        # track the generation of the agent
        self.generation = 0

        # Learning rate schedulers
        self.policy_scheduler = ExponentialLR(self.policy_optimizer, gamma=self.lr_decay_rate)
        self.target_scheduler = ExponentialLR(self.target_optimizer, gamma=self.lr_decay_rate)

    def store_transition(self, state, action, reward, state_, done):
        self.memory.store_transition(state, action, reward, state_, done)

    def replace_target_network(self):
        if self.learn_step_counter % self.replace_target_cnt == 0:
            self.q_target.load_state_dict(self.q_policy.state_dict())

    def decrement_epsilon(self):
        self.epsilon = self.epsilon * self.epsilon_dec if self.epsilon > self.epsilon_min else self.epsilon_min

    def learn(self):
        # track the time it takes to learn
        start_time = time.time()
        print('\n', "-" * 100)
        self.replace_target_network()

        # Set the policy network to training mode
        self.q_policy.train()

        states, actions, rewards, states_, dones = self.memory.sample_buffer(self.batch_size)

        # Initialize eligibility traces
        eligibility_traces = torch.zeros((self.batch_size, self.n_actions)).to(self.device)

        for epoch in range(self.n_epochs):  # Loop over epochs
            # Calculate the number of mini-batches
            num_mini_batches = max(self.batch_size // self.mini_batch_size, 1)

            for mini_batch in range(num_mini_batches):
                # Calculate the start and end indices of the mini-batch
                start = mini_batch * self.mini_batch_size
                end = min((mini_batch + 1) * self.mini_batch_size, self.batch_size)

                # Convert the mini-batch to tensors
                mini_states = torch.tensor(states[start:end], dtype=torch.float).to(self.device)
                mini_actions = torch.tensor(actions[start:end]).to(self.device)
                mini_rewards = torch.tensor(rewards[start:end], dtype=torch.float).to(self.device)
                mini_states_ = torch.tensor(states_[start:end], dtype=torch.float).to(self.device)
                mini_dones = torch.tensor(dones[start:end], dtype=torch.bool).to(self.device)

                q_pred = self.q_policy(mini_states).gather(1, mini_actions.unsqueeze(-1)).squeeze(-1)
                q_next = self.q_target(mini_states_).detach()
                q_eval = self.q_policy(mini_states_).detach()

                max_actions = torch.argmax(q_eval, dim=1)
                q_next[mini_dones] = 0.0
                q_target = mini_rewards + self.gamma * q_next.gather(1, max_actions.unsqueeze(-1)).squeeze(-1)

                td_error = q_target - q_pred
                eligibility_traces_mini = eligibility_traces[start:end]  # work with the correct segment
                eligibility_traces_mini = self.gamma * self.lambda_ * eligibility_traces_mini
                eligibility_traces_mini.scatter_(1, mini_actions.unsqueeze(-1), 1)

                expanded_td_error = td_error.unsqueeze(1).expand_as(eligibility_traces_mini)
                loss = (expanded_td_error.pow(2) * eligibility_traces_mini).mean()

                l1_penalty = sum(p.abs().sum() for p in self.q_policy.parameters())
                total_loss = loss + self.l1_lambda * l1_penalty

                self.policy_optimizer.zero_grad()
                total_loss.backward()
                self.policy_optimizer.step()

        # Decay learning rate
        self.policy_scheduler.step()
        self.target_scheduler.step()

        self.learn_step_counter += 1  # Increment the learn step counter
        self.decrement_epsilon()  # Decrement the exploration rate

        # Clear memory after learning
        self.memory.clear_memory()

        # Increment generation of the agent
        self.generation += 1

        # track the time it takes to learn
        end_time = time.time()
        episode_time = end_time - start_time

        # print the time it takes to learn
        print(f"Learning of agent generation {self.generation} completed in {episode_time} seconds")
        print("-" * 100)

    @torch.no_grad()
    def choose_action(self, observation, current_position):
        # Epsilon-greedy action selection
        if np.random.random() > self.epsilon:
            # Convert the observation to a numpy array if it's not already
            if not isinstance(observation, np.ndarray):
                observation = np.array(observation)

            # Reshape the observation to (1, -1) which means 1 row and as many columns as necessary
            observation = observation.reshape(1, -1)

            # Convert the numpy array to a tensor
            state = torch.tensor(observation, dtype=torch.float).to(self.device)

            actions = self.q_policy(state)
            action = torch.argmax(actions).item()
        # if the random number is less than epsilon, take a random action
        else:
            action = np.random.choice(self.action_space)

        return action

    @torch.no_grad()
    def get_action_q_values(self, observation, current_position):
        """
        Returns the Q-values of each action for a given observation.
        """
        if not isinstance(observation, np.ndarray):
            observation = np.array(observation)

        observation = observation.reshape(1, -1)
        state = torch.tensor(observation, dtype=torch.float).to(self.device)

        q_values = self.q_policy(state)

        return q_values.cpu().numpy()

    @torch.no_grad()
    def choose_best_action(self, observation, current_position):
        """
        Selects the best action based on the highest Q-value without exploration.
        """
        q_values = self.get_action_q_values(observation)
        best_action = np.argmax(q_values)
        return best_action

    def get_epsilon(self):
        return self.epsilon

    @torch.no_grad()
    def get_action_probabilities(self, observation, current_position):
        """
        Returns the probabilities of each action for a given observation.
        Converts Q values to a probability distribution using the softmax function.
        """
        if not isinstance(observation, np.ndarray):
            observation = np.array(observation)
        observation = np.append(observation, current_position)

        observation = observation.reshape(1, -1)
        state = torch.tensor(observation, dtype=torch.float).to(self.device)

        q_values = self.q_policy(state)
        probabilities = F.softmax(q_values, dim=1).cpu().numpy()

        return probabilities.flatten()

    def get_name(self):
        """
        Returns the class name of the instance.
        """
        return self.__class__.__name__


if __name__ == '__main__':
    # time the execution
    start_time_X = time.time()
    # Set seeds for reproducibility
    torch.manual_seed(0)
    np.random.seed(0)
    random.seed(0)

    # Example usage
    # Stock market variables
    df = load_data_parallel(['EURUSD', 'USDJPY', 'EURJPY', 'GBPUSD'], '1D')

    indicators = [
        {"indicator": "RSI", "mkf": "EURUSD", "length": 14},
        {"indicator": "ATR", "mkf": "EURUSD", "length": 24},
        {"indicator": "MACD", "mkf": "EURUSD"},
        {"indicator": "Stochastic", "mkf": "EURUSD"}, ]

    return_indicators = [
        {"price_type": "Close", "mkf": "EURUSD"},
        {"price_type": "Close", "mkf": "USDJPY"},
        {"price_type": "Close", "mkf": "EURJPY"},
        {"price_type": "Close", "mkf": "GBPUSD"},
    ]
    add_indicators(df, indicators)
    add_returns(df, return_indicators)

    add_time_sine_cosine(df, '1W')

    df = df.dropna()
    # data before 2006 has some missing values ie gaps in the data, also in march, april 2023 there are some gaps
    start_date = '2007-01-01'  # worth to keep 2008 as it was a financial crisis
    validation_date = '2018-12-31'
    test_date = '2022-01-01'
    df_train, df_validation, df_test = df[start_date:validation_date], df[validation_date:test_date], df[test_date:]

    variables = [
        {"variable": ("Close", "USDJPY"), "edit": "normalize"},
        {"variable": ("Close", "EURUSD"), "edit": "normalize"},
        {"variable": ("Close", "EURJPY"), "edit": "normalize"},
        {"variable": ("Close", "GBPUSD"), "edit": "normalize"},
        {"variable": ("RSI_14", "EURUSD"), "edit": "standardize"},
        {"variable": ("ATR_24", "EURUSD"), "edit": "standardize"},
        {"variable": ("sin_time_1W", ""), "edit": None},
        {"variable": ("cos_time_1W", ""), "edit": None},
        {"variable": ("Returns_Close", "EURUSD"), "edit": None},
        {"variable": ("Returns_Close", "USDJPY"), "edit": None},
        {"variable": ("Returns_Close", "EURJPY"), "edit": None},
        {"variable": ("Returns_Close", "GBPUSD"), "edit": None},
    ]

    tradable_markets = 'EURUSD'
    window_size = '1Y'
    starting_balance = 10000
    look_back = 40
    # Provision is the cost of trading, it is a percentage of the trade size, current real provision on FOREX is 0.0001
    provision = 0.0002  # 0.001, cant be too high as it would not learn to trade

    # Environment parameters
    leverage = 1
    num_episodes = 1000  # 100

    # Instantiate the agent
    agent = DDQN_Agent(input_dims=len(variables) * look_back + 1,  # +1 for the current position
                       n_actions=3,  # buy, sell, hold
                       n_epochs=1,  # number of epochs 10
                       mini_batch_size=4096,  # mini batch size 128
                       policy_alpha=0.0005,  # learning rate for the policy network
                       target_alpha=0.00005,  # learning rate for the target network
                       gamma=0.5,  # discount factor 0.99
                       epsilon=1.0,  # initial epsilon 1.0
                       epsilon_dec=0.975,  # epsilon decay rate 0.99
                       epsilon_end=0,  # minimum epsilon  0
                       mem_size=100000000,   # memory size 100000
                       batch_size=4096,  # batch size  1024
                       replace=10,  # replace target network count 10
                       weight_decay=0.000005,  # Weight decay
                       l1_lambda=0.00000005,  # L1 regularization lambda
                       lr_decay_rate=0.9999,   # Learning rate decay rate
                       premium_gamma=0.5,  # Discount factor for the alternative rewards
                       lambda_=0.25,  # Lambda for TD(lambda) learning
                       )

    total_rewards, episode_durations, total_balances = [], [], []
    episode_probabilities = {'train': [], 'validation': [], 'test': []}

    index = pd.MultiIndex.from_product([range(num_episodes), ['validation', 'test']], names=['episode', 'dataset'])
    columns = ['Final Balance', 'Dataset Index']
    backtest_results = pd.DataFrame(index=index, columns=columns)

    window_size_2 = '1Y'
    test_rolling_datasets = rolling_window_datasets(df_test, window_size=window_size_2, look_back=look_back)
    val_rolling_datasets = rolling_window_datasets(df_validation, window_size=window_size_2, look_back=look_back)

    # Generate index labels for each rolling window dataset
    val_labels = generate_index_labels(val_rolling_datasets, 'validation')
    test_labels = generate_index_labels(test_rolling_datasets, 'test')
    all_labels = val_labels + test_labels

    # Rolling DF
    rolling_datasets = rolling_window_datasets(df_train, window_size=window_size, look_back=look_back)
    dataset_iterator = cycle(rolling_datasets)

    backtest_results, probs_dfs, balances_dfs = {}, {}, {}
    generation = 0

    for episode in tqdm(range(num_episodes)):
        window_df = next(dataset_iterator)
        dataset_index = episode % len(rolling_datasets)

        print(f"\nEpisode {episode + 1}: Learning from dataset with Start Date = {window_df.index.min()}, End Date = {window_df.index.max()}, len = {len(window_df)}")

        # Create a new environment with the randomly selected window's data
        env = Trading_Environment_Basic(window_df, look_back=look_back, variables=variables,
                                        tradable_markets=tradable_markets, provision=provision,
                                        initial_balance=starting_balance, leverage=leverage,
                                        reward_function=reward_calculation)

        observation = env.reset()
        done = False
        start_time = time.time()
        initial_balance = env.balance
        observation = np.append(observation, 0)

        while not done:
            action = agent.choose_action(observation, env.current_position)
            observation_, reward, done, info = env.step(action)
            observation_ = np.append(observation_, env.current_position)
            agent.store_transition(observation, action, reward, observation_, done)
            observation = observation_

            # Check if enough data is collected or if the dataset ends
            if agent.memory.mem_cntr >= agent.batch_size:
                agent.learn()
                agent.memory.clear_memory()

            if generation < agent.generation:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = []
                    for df, label in zip(val_rolling_datasets + test_rolling_datasets, val_labels + test_labels):
                        future = executor.submit(BF.backtest_wrapper, 'DQN', df, agent, tradable_markets, look_back,
                                                 variables, provision, starting_balance, leverage,
                                                 Trading_Environment_Basic, reward_calculation)
                        futures.append((future, label))

                    for future, label in futures:
                        (balance, total_reward, number_of_trades, probs_df, action_df, sharpe_ratio, max_drawdown,
                         sortino_ratio, calmar_ratio, cumulative_returns, balances, provision_sum) = future.result()
                        result_data = {
                            'Agent generation': agent.generation,
                            'Label': label,
                            'Provision_sum': provision_sum,
                            'Final Balance': balance,
                            'Total Reward': total_reward,
                            'Number of Trades': number_of_trades,
                            'Sharpe Ratio': sharpe_ratio,
                            'Max Drawdown': max_drawdown,
                            'Sortino Ratio': sortino_ratio,
                            'Calmar Ratio': calmar_ratio
                        }
                        key = (agent.generation, label)

                        if key not in backtest_results:
                            backtest_results[key] = []

                        backtest_results[key].append(result_data)

                        # Store probabilities and balances for plotting
                        probs_dfs[(agent.generation, label)] = probs_df
                        balances_dfs[(agent.generation, label)] = balances

                    generation = agent.generation
                    print(f"Backtesting completed for {agent.get_name()} generation {generation}")

        # results
        end_time = time.time()
        episode_time = end_time - start_time
        total_rewards.append(env.reward_sum)
        episode_durations.append(episode_time)
        total_balances.append(env.balance)

        print(
            f"Completed learning fro selected window in episode {episode + 1}: Total Reward: {env.reward_sum}, Total Balance: {env.balance:.2f}, Duration: {episode_time:.2f} seconds, Agent Epsilon: {agent.get_epsilon():.4f}")
        print(f'Final Balance of Buy and Hold benchmark agent: ', starting_balance * (1 + (
                    window_df[('Close', tradable_markets)].iloc[-1] - window_df[('Close', tradable_markets)].iloc[
                look_back]) / window_df[('Close', tradable_markets)].iloc[look_back] * leverage))
        # TODO do this as cached function with benchmark agents and df as input

    #save_model(agent.q_policy, base_dir="saved models", sub_dir="DDQN", file_name="q_policy")  # TODO repair save_model
    #save_model(agent.q_target, base_dir="saved models", sub_dir="DDQN", file_name="q_target")  # TODO repair save_model

    # prepare benchmark results
    # Instantiate the Buy_and_hold_Agent
    # prepare benchmark results
    buy_and_hold_agent = Buy_and_hold_Agent()
    sell_and_hold_agent = Sell_and_hold_Agent()

    # Run backtesting for both agents
    bah_results, _, benchmark_BAH = BF.run_backtesting(
        buy_and_hold_agent, 'BAH', val_rolling_datasets + test_rolling_datasets, val_labels + test_labels,
        BF.backtest_wrapper, tradable_markets, look_back, variables, provision, starting_balance, leverage,
        Trading_Environment_Basic, reward_calculation, workers=4)

    sah_results, _, benchmark_SAH = BF.run_backtesting(
        sell_and_hold_agent, 'SAH', val_rolling_datasets + test_rolling_datasets, val_labels + test_labels,
        BF.backtest_wrapper, tradable_markets, look_back, variables, provision, starting_balance, leverage,
        Trading_Environment_Basic, reward_calculation, workers=4)

    bah_results_prepared = prepare_backtest_results(bah_results, 'BAH')
    sah_results_prepared = prepare_backtest_results(sah_results, 'SAH')

    sah_results_prepared = sah_results_prepared.drop(('', 'Agent Generation'), axis=1)  # drop the agent generation column
    bah_results_prepared = bah_results_prepared.drop(('', 'Agent Generation'), axis=1)  # drop the agent generation column

    # Merge BAH and SAH results on 'Label'
    new_backtest_results = pd.merge(bah_results_prepared, sah_results_prepared, on=[('', 'Label')], how='outer')

    backtest_results = prepare_backtest_results(backtest_results, agent.get_name())
    backtest_results = pd.merge(backtest_results, new_backtest_results, on=[('', 'Label')], how='outer')
    backtest_results = backtest_results.set_index([('', 'Agent Generation')])

    label_series = backtest_results[('', 'Label')]
    backtest_results = backtest_results.drop(('', 'Label'), axis=1)
    backtest_results['Label'] = label_series

    print(backtest_results)

    from backtest.plots.generation_plot import plot_results, plot_total_rewards, plot_total_balances
    from backtest.plots.OHLC_probability_plot import PnL_generation_plot, Probability_generation_plot, PnL_generations, Reward_generations

    plot_results(backtest_results, [(agent.get_name(), 'Final Balance'),
            (agent.get_name(), 'Number of Trades'), (agent.get_name(), 'Total Reward')], agent.get_name())
    plot_total_rewards(total_rewards, agent.get_name())
    plot_total_balances(total_balances, agent.get_name())

    PnL_generation_plot(balances_dfs, [benchmark_BAH, benchmark_SAH], port_number=8060)
    Probability_generation_plot(probs_dfs, port_number=8061)  # TODO add here OHLC
    PnL_generations(backtest_results, port_number=8062)
    Reward_generations(backtest_results, port_number=8063)

    print('end')
