"""
Transformer based PPO agent for trading version 1.2

# TODO LIST
- Multiple Actors (Parallelization): Implement multiple actors that collect data in parallel. This can significantly speed up data collection and can lead to more diverse experience, helping in stabilizing training.
- Hyperparameter Tuning: Use techniques like grid search, random search, or Bayesian optimization to find the best set of hyperparameters.
- Noise Injection for Exploration: Inject noise into the policy or action space to encourage exploration. This can be particularly effective in continuous action spaces.
- Automated Architecture Search: Use techniques like neural architecture search (NAS) to automatically find the most suitable network architecture.
- HRL (Hierarchical Reinforcement Learning): Use hierarchical reinforcement learning to learn sub-policies for different tasks. Master agent would distribute capital among sub-agents for different tickers.

"""
import numpy as np
import pandas as pd
import time
import torch
import torch.nn as nn
import torch.optim as optim
from tqdm import tqdm
import random
import math
import gym
from gym import spaces
from itertools import cycle
from concurrent.futures import ThreadPoolExecutor
import torch
import torch.nn as nn
import torch.optim as optim
from torch.nn import TransformerEncoder, TransformerEncoderLayer

from data.function.load_data import load_data_parallel
from data.function.rolling_window import rolling_window_datasets
from data.function.edit import normalize_data, standardize_data
from technical_analysys.add_indicators import add_indicators, add_returns, add_log_returns, add_time_sine_cosine
import backtest.backtest_functions.functions as BF
from functions.utilis import save_actor_critic_model


# Set seeds for reproducibility
torch.manual_seed(0)
np.random.seed(0)
random.seed(0)

class PPOMemory:
    def __init__(self, batch_size, device):
        self.states = None
        self.probs = None
        self.actions = None
        self.vals = None
        self.rewards = None
        self.dones = None
        self.static_states = None
        self.batch_size = batch_size
        self.clear_memory()

        self.device = device

    def generate_batches(self):
        n_states = len(self.states)
        batch_start = torch.arange(0, n_states, self.batch_size)
        indices = torch.arange(n_states, dtype=torch.int64)
        indices = indices[torch.randperm(n_states)]  # Shuffle indices
        batches = [indices[i:i + self.batch_size] for i in batch_start]

        # Include static states in the generated batches
        return self.states, self.actions, self.probs, self.vals, self.rewards, self.dones, self.static_states, batches

    def store_memory(self, state, action, probs, vals, reward, done, static_state):
        self.states.append(torch.tensor(state, dtype=torch.float).unsqueeze(0))
        self.actions.append(torch.tensor(action, dtype=torch.long).unsqueeze(0))
        self.probs.append(torch.tensor(probs, dtype=torch.float).unsqueeze(0))
        self.vals.append(torch.tensor(vals, dtype=torch.float).unsqueeze(0))
        self.rewards.append(torch.tensor(reward, dtype=torch.float).unsqueeze(0))
        self.dones.append(torch.tensor(done, dtype=torch.bool).unsqueeze(0))
        self.static_states.append(torch.tensor(static_state, dtype=torch.float).unsqueeze(0))

    def clear_memory(self):
        self.states = []
        self.probs = []
        self.actions = []
        self.vals = []
        self.rewards = []
        self.dones = []
        self.static_states = []

    def stack_tensors(self):
        self.states = torch.cat(self.states, dim=0).to(self.device)
        self.actions = torch.cat(self.actions, dim=0).to(self.device)
        self.probs = torch.cat(self.probs, dim=0).to(self.device)
        self.vals = torch.cat(self.vals, dim=0).to(self.device)
        self.rewards = torch.cat(self.rewards, dim=0).to(self.device)
        self.dones = torch.cat(self.dones, dim=0).to(self.device)
        self.static_states = torch.cat(self.static_states, dim=0).to(self.device)


class ActorNetwork(nn.Module):
    def __init__(self, n_actions, input_dims, n_heads=4, n_layers=3, dropout_rate=0.1, static_input_dims=1, fc_dims=[256, 128]):
        super(ActorNetwork, self).__init__()
        self.input_dims = input_dims
        self.static_input_dims = static_input_dims
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.dropout_rate = dropout_rate

        encoder_layers = TransformerEncoderLayer(d_model=input_dims, nhead=n_heads, dropout=dropout_rate, batch_first=True)
        self.transformer_encoder = TransformerEncoder(encoder_layer=encoder_layers, num_layers=n_layers)

        self.max_position_embeddings = 512
        self.positional_encoding = nn.Parameter(torch.zeros(1, self.max_position_embeddings, input_dims))
        self.fc_static = nn.Linear(static_input_dims, input_dims)

        # Create the fully connected layers dynamically based on fc_dims
        self.fc_layers = nn.ModuleList()
        last_dim = input_dims * 2  # Initial size combining transformer and static input features
        for fc_dim in fc_dims:
            self.fc_layers.append(nn.Linear(last_dim, fc_dim))
            self.fc_layers.append(nn.ReLU())
            last_dim = fc_dim
        self.fc_layers.append(nn.Linear(last_dim, n_actions))  # Output layer

        self.softmax = nn.Softmax(dim=-1)

    def forward(self, dynamic_state, static_state):
        batch_size, seq_length, _ = dynamic_state.size()
        positional_encoding = self.positional_encoding[:, :seq_length, :].expand(batch_size, -1, -1)

        dynamic_state = dynamic_state + positional_encoding
        transformer_out = self.transformer_encoder(dynamic_state)

        static_state_encoded = self.fc_static(static_state.unsqueeze(1))
        combined_features = torch.cat((transformer_out[:, -1, :], static_state_encoded.squeeze(1)), dim=1)

        # Pass combined features through the FC layers
        for layer in self.fc_layers:
            combined_features = layer(combined_features)

        return self.softmax(combined_features)


class CriticNetwork(nn.Module):
    def __init__(self, input_dims, n_heads=4, n_layers=3, dropout_rate=0.1, static_input_dims=1, fc_dims=[256, 128]):
        super(CriticNetwork, self).__init__()
        self.input_dims = input_dims
        self.static_input_dims = static_input_dims
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.dropout_rate = dropout_rate

        encoder_layers = TransformerEncoderLayer(d_model=input_dims, nhead=n_heads, dropout=dropout_rate, batch_first=True)
        self.transformer_encoder = TransformerEncoder(encoder_layer=encoder_layers, num_layers=n_layers)

        self.max_position_embeddings = 512
        self.positional_encoding = nn.Parameter(torch.zeros(1, self.max_position_embeddings, input_dims))
        self.fc_static = nn.Linear(static_input_dims, input_dims)

        # Create the fully connected layers dynamically based on fc_dims
        self.fc_layers = nn.ModuleList()
        last_dim = input_dims * 2  # Initial size combining transformer and static input features
        for fc_dim in fc_dims:
            self.fc_layers.append(nn.Linear(last_dim, fc_dim))
            self.fc_layers.append(nn.ReLU())
            last_dim = fc_dim
        self.fc_layers.append(nn.Linear(last_dim, 1))  # Output layer

    def forward(self, dynamic_state, static_state):
        batch_size, seq_length, _ = dynamic_state.size()
        positional_encoding = self.positional_encoding[:, :seq_length, :].expand(batch_size, -1, -1)

        dynamic_state = dynamic_state + positional_encoding
        transformer_out = self.transformer_encoder(dynamic_state)

        static_state_encoded = self.fc_static(static_state.unsqueeze(1))
        combined_features = torch.cat((transformer_out[:, -1, :], static_state_encoded.squeeze(1)), dim=1)

        # Pass combined features through the FC layers
        for layer in self.fc_layers:
            combined_features = layer(combined_features)

        return combined_features

class Transformer_PPO_Agent:
    def __init__(self, n_actions, input_dims, gamma=0.95, alpha=0.001, gae_lambda=0.9, policy_clip=0.2, batch_size=1024, n_epochs=20, mini_batch_size=128, entropy_coefficient=0.01, weight_decay=0.0001, l1_lambda=1e-5, static_input_dims=1):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"Using device: {self.device}")
        self.gamma = gamma
        self.policy_clip = policy_clip
        self.n_epochs = n_epochs
        self.gae_lambda = gae_lambda
        self.mini_batch_size = mini_batch_size
        self.entropy_coefficient = entropy_coefficient
        self.l1_lambda = l1_lambda

        # Initialize the actor and critic networks with static input dimensions
        self.actor = ActorNetwork(n_actions, input_dims, static_input_dims=static_input_dims).to(self.device)
        self.critic = CriticNetwork(input_dims, static_input_dims=static_input_dims).to(self.device)
        self.actor_optimizer = optim.Adam(self.actor.parameters(), lr=alpha, weight_decay=weight_decay)
        self.critic_optimizer = optim.Adam(self.critic.parameters(), lr=alpha, weight_decay=weight_decay)

        self.memory = PPOMemory(batch_size, self.device)

    def store_transition(self, state, action, probs, vals, reward, done, static_state):
        # Include static_state in the memory storage
        self.memory.store_memory(state, action, probs, vals, reward, done, static_state)

    def learn(self):
        print('Learning... CHECK')
        self.actor.train()
        self.critic.train()

        self.memory.stack_tensors()

        for _ in range(self.n_epochs):
            # Generating the data for the entire batch, including static states
            state_arr, action_arr, old_prob_arr, vals_arr, reward_arr, dones_arr, static_states_arr, batches = self.memory.generate_batches()

            # Convert arrays to tensors and move to the device
            state_arr = state_arr.clone().detach().to(self.device)
            action_arr = action_arr.clone().detach().to(self.device)
            old_prob_arr = old_prob_arr.clone().detach().to(self.device)
            vals_arr = vals_arr.clone().detach().to(self.device)
            reward_arr = reward_arr.clone().detach().to(self.device)
            dones_arr = dones_arr.clone().detach().to(self.device)
            static_states_arr = static_states_arr.clone().detach().to(self.device)  # Static states

            # Compute advantages and discounted rewards
            advantages, discounted_rewards = self.compute_discounted_rewards(reward_arr, vals_arr.cpu().numpy(), dones_arr)
            advantages = advantages.clone().detach().to(self.device)
            discounted_rewards = discounted_rewards.clone().detach().to(self.device)

            # Creating mini-batches and training
            num_samples = len(state_arr)
            indices = np.arange(num_samples)
            np.random.shuffle(indices)

            for start_idx in range(0, num_samples, self.mini_batch_size):
                minibatch_indices = indices[start_idx:start_idx + self.mini_batch_size]

                batch_states = state_arr[minibatch_indices].clone().detach().to(self.device)
                batch_actions = action_arr[minibatch_indices].clone().detach().to(self.device)
                batch_old_probs = old_prob_arr[minibatch_indices].clone().detach().to(self.device)
                batch_advantages = advantages[minibatch_indices]
                batch_returns = discounted_rewards[minibatch_indices]
                batch_static_states = static_states_arr[minibatch_indices].clone().detach().to(self.device)  # Static states for the batch

                self.actor_optimizer.zero_grad()
                self.critic_optimizer.zero_grad()

                # Calculate actor and critic losses, include static states in forward passes
                new_probs, dist_entropy, actor_loss, critic_loss = self.calculate_loss(batch_states, batch_actions, batch_old_probs, batch_advantages, batch_returns, batch_static_states)

                # Perform backpropagation and optimization steps
                actor_loss.backward()
                self.actor_optimizer.step()

                critic_loss.backward()
                self.critic_optimizer.step()

        # Clear memory after learning
        self.memory.clear_memory()

    def calculate_loss(self, batch_states, batch_actions, batch_old_probs, batch_advantages, batch_returns, batch_static_states):
        if batch_states.dim() == 2:
            batch_states = batch_states.unsqueeze(1)

        # Actor loss calculations
        new_probs = self.actor(batch_states, batch_static_states)
        dist = torch.distributions.Categorical(new_probs)
        new_log_probs = dist.log_prob(batch_actions)
        prob_ratios = torch.exp(new_log_probs - batch_old_probs)
        surr1 = prob_ratios * batch_advantages
        surr2 = torch.clamp(prob_ratios, 1.0 - self.policy_clip, 1.0 + self.policy_clip) * batch_advantages
        actor_loss = -torch.min(surr1, surr2).mean() - self.entropy_coefficient * dist.entropy().mean()

        # Critic loss calculations
        critic_values = self.critic(batch_states, batch_static_states).squeeze(-1)
        critic_loss = (batch_returns - critic_values).pow(2).mean()

        return new_probs, dist.entropy(), actor_loss, critic_loss


    def compute_discounted_rewards(self, rewards, values, dones):
        n = len(rewards)
        discounted_rewards = torch.zeros_like(rewards)
        advantages = torch.zeros_like(rewards)
        last_gae_lam = 0

        # Convert 'dones' to a float tensor
        dones = dones.float()

        for t in reversed(range(n)):
            if t == n - 1:
                next_non_terminal = 1.0 - dones[t]
                next_values = 0
            else:
                next_non_terminal = 1.0 - dones[t + 1]
                next_values = values[t + 1]
            delta = rewards[t] + self.gamma * next_values * next_non_terminal - values[t]
            last_gae_lam = delta + self.gamma * self.gae_lambda * next_non_terminal * last_gae_lam
            advantages[t] = last_gae_lam
            discounted_rewards[t] = advantages[t] + values[t]

        return advantages, discounted_rewards

    def choose_action(self, observation, static_input):
        # Ensure observation is a NumPy array
        if not isinstance(observation, np.ndarray):
            observation = np.array(observation)

        # Reshape observation to [1, sequence length, feature dimension]
        observation = observation.reshape(1, -1, observation.shape[-1])

        # Convert observation and static_input to tensors and move them to the appropriate device
        state = torch.tensor(observation, dtype=torch.float).to(self.device)
        static_input_tensor = torch.tensor([static_input], dtype=torch.float).to(self.device)

        # Ensure state has the correct 3D shape: [batch size, sequence length, feature dimension]
        if state.dim() != 3:  # Add missing dimensions if necessary
            state = state.view(1, -1, state.size(-1))

        # Pass the state and static_input_tensor through the actor network to get the action probabilities
        probs = self.actor(state, static_input_tensor)

        # Create a categorical distribution over the list of probabilities of actions
        dist = torch.distributions.Categorical(probs)

        # Sample an action from the distribution
        action = dist.sample()

        # Calculate the log probability of the action in the distribution
        log_prob = dist.log_prob(action)

        # Pass the state and static_input_tensor through the critic network to get the state value
        value = self.critic(state, static_input_tensor)

        # Return the sampled action, its log probability, and the state value
        # Convert tensors to Python numbers using .item()
        return action.item(), log_prob.item(), value.item()

    def get_action_probabilities(self, dynamic_state, static_state):
        """
        Returns the probabilities of each action for a given observation.

        Parameters:
        - dynamic_state: The dynamic part of the state (e.g., market indicators).
        - static_state: The static part of the state (e.g., current market position).
        """
        dynamic_state = torch.tensor(dynamic_state, dtype=torch.float).unsqueeze(0).to(
            self.device)  # Add batch dimension
        static_state = torch.tensor(static_state, dtype=torch.float).unsqueeze(0).to(
            self.device)  # Static state may not need sequence dimension but still needs batch dimension

        with torch.no_grad():
            probs = self.actor(dynamic_state, static_state)
        return probs.cpu().numpy()

    def choose_best_action(self, dynamic_state, static_state):
        """
        Selects the best action based on the current policy without exploration by using
        the probabilities from get_action_probabilities function.

        Parameters:
        - dynamic_state: The dynamic part of the state (e.g., market indicators).
        - static_state: The static part of the state (e.g., current market position).
        """
        action_probs = self.get_action_probabilities(dynamic_state, static_state)

        # Use np.argmax to select the action with the highest probability
        best_action = np.argmax(action_probs)

        return best_action

    def get_name(self):
        """
        Returns the class name of the instance.
        """
        return self.__class__.__name__



class Trading_Environment_Basic(gym.Env):
    def __init__(self, df, look_back=20, variables=None, tradable_markets='EURUSD', provision=0.0001, initial_balance=10000, leverage=1, reward_scaling=1):
        super(Trading_Environment_Basic, self).__init__()
        self.df = df.reset_index(drop=True)
        self.look_back = look_back
        self.initial_balance = initial_balance
        self.current_position = 0  # This is a static part of the state
        self.variables = variables
        self.tradable_markets = tradable_markets
        self.provision = provision
        self.leverage = leverage
        self.reward_scaling = reward_scaling

        # Define action space: 0 (sell), 1 (hold), 2 (buy)
        self.action_space = spaces.Discrete(3)

        self.reset()

    def calculate_input_dims(self):
        num_variables = len(self.variables)  # Number of variables
        input_dims = num_variables * self.look_back  # Variables times look_back
        return input_dims

    def reset(self, observation=None):
        if observation is not None:
            self.current_step = observation + self.look_back
        else:
            self.current_step = self.look_back

        self.balance = self.initial_balance
        self.done = False
        return self._next_observation()

    def _next_observation(self):
        start = max(self.current_step - self.look_back, 0)
        end = self.current_step

        # Apply scaling based on 'edit' property of each variable
        scaled_observation = []
        for variable in self.variables:
            data = self.df[variable['variable']].iloc[start:end].values
            if variable['edit'] == 'standardize':
                scaled_data = standardize_data(data)
            elif variable['edit'] == 'normalize':
                scaled_data = normalize_data(data)
            else:
                scaled_data = data

            scaled_observation.extend(scaled_data)
        return np.array(scaled_observation)


    def step(self, action): # TODO: Check if this is correct
        action_mapping = {0: -1, 1: 0, 2: 1}
        mapped_action = action_mapping[action]

        current_price = self.df[('Close', self.tradable_markets)].iloc[self.current_step]
        next_price = self.df[('Close', self.tradable_markets)].iloc[self.current_step + 1]

        log_return = math.log(next_price / current_price) if current_price != 0 else 0
        reward = log_return * mapped_action * self.leverage

        # Calculate cost based on action and current position
        if mapped_action != self.current_position:
            if abs(mapped_action) == 1:
                provision = math.log(1 - self.provision)
            else:
                provision = 0
        else:
            provision = 0

        reward += provision

        self.balance *= math.exp(reward)  # Scale reward for learning stability
        self.current_position = mapped_action
        self.current_step += 1
        """
        multiple reward by X to make it more significant and to make it easier for the agent to learn, 
        without this the agent would not learn as the reward is too close to 0
        """
        final_reward = reward * self.reward_scaling

        self.done = self.current_step >= len(self.df) - 1

        return self._next_observation(), final_reward, self.done, {}

# Example usage
# Stock market variables
if __name__ == '__main__':
    # Example usage
    # Stock market variables
    df = load_data_parallel(['EURUSD', 'USDJPY', 'EURJPY', 'GBPUSD'], '1D')

    indicators = [
        {"indicator": "RSI", "mkf": "EURUSD", "length": 14},
        {"indicator": "ATR", "mkf": "EURUSD", "length": 24},
        {"indicator": "MACD", "mkf": "EURUSD"},
        {"indicator": "Stochastic", "mkf": "EURUSD"},]

    add_indicators(df, indicators)
    add_time_sine_cosine(df, '1W')
    add_time_sine_cosine(df, '1M')
    df[("sin_time_1W", "")] = df[("sin_time_1W", "")]/2 + 0.5
    df[("cos_time_1W", "")] = df[("cos_time_1W", "")]/2 + 0.5
    df[("sin_time_1M", "")] = df[("sin_time_1M", "")]/2 + 0.5
    df[("cos_time_1M", "")] = df[("cos_time_1M", "")]/2 + 0.5
    df[("RSI_14", "EURUSD")] = df[("RSI_14", "EURUSD")]/100

    df = df.dropna()
    start_date = '2008-01-01'
    validation_date = '2022-01-01'
    test_date = '2023-01-01'
    df_train = df[start_date:validation_date]
    df_validation = df[validation_date:test_date]
    df_test = df[test_date:]
    variables = [
        {"variable": ("Close", "USDJPY"), "edit": "normalize"},
        {"variable": ("Close", "EURUSD"), "edit": "normalize"},
        {"variable": ("Close", "EURJPY"), "edit": "normalize"},
        {"variable": ("Close", "GBPUSD"), "edit": "normalize"},
        {"variable": ("RSI_14", "EURUSD"), "edit": "normalize"},
        {"variable": ("sin_time_1W", ""), "edit": None},
        {"variable": ("cos_time_1W", ""), "edit": None},
    ]

    tradable_markets = 'EURUSD'
    window_size = '1Y'
    starting_balance = 10000
    look_back = 20
    # Provision is the cost of trading, it is a percentage of the trade size, current real provision on FOREX is 0.0001
    provision = 0.0001  # 0.001, cant be too high as it would not learn to trade

    # Training parameters
    batch_size = 1024
    epochs = 1  # 40
    mini_batch_size = 128
    leverage = 1
    weight_decay = 0.00001
    l1_lambda = 1e-7
    reward_scaling = 1000
    num_episodes = 500  # 100
    # Create the environment
    env = Trading_Environment_Basic(df_train, look_back=look_back, variables=variables, tradable_markets=tradable_markets, provision=provision, initial_balance=starting_balance, leverage=leverage, reward_scaling=reward_scaling)
    agent = Transformer_PPO_Agent(n_actions=env.action_space.n,
                        input_dims=env.calculate_input_dims(),
                        gamma=0.9,
                        alpha=0.0005,  # learning rate for actor network
                        gae_lambda=0.8,  # lambda for generalized advantage estimation
                        policy_clip=0.15,  # clip parameter for PPO
                        entropy_coefficient=0.5,  # higher entropy coefficient encourages exploration
                        batch_size=batch_size,
                        n_epochs=epochs,
                        mini_batch_size=mini_batch_size,
                        weight_decay=weight_decay,
                        l1_lambda=l1_lambda,
                        static_input_dims=1)

    total_rewards = []
    episode_durations = []
    total_balances = []
    episode_probabilities = {'train': [], 'validation': [], 'test': []}

    index = pd.MultiIndex.from_product([range(num_episodes), ['validation', 'test']], names=['episode', 'dataset'])
    columns = ['Final Balance', 'Dataset Index']
    backtest_results = pd.DataFrame(index=index, columns=columns)

    # Rolling DF
    rolling_datasets = rolling_window_datasets(df_train, window_size=window_size,  look_back=look_back)
    dataset_iterator = cycle(rolling_datasets)

    for episode in tqdm(range(num_episodes)):
        window_df = next(dataset_iterator)
        dataset_index = episode % len(rolling_datasets)

        print(f"\nEpisode {episode + 1}: Learning from dataset with Start Date = {window_df.index.min()}, End Date = {window_df.index.max()}, len = {len(window_df)}")
        # Create a new environment with the randomly selected window's data
        env = Trading_Environment_Basic(window_df, look_back=look_back, variables=variables, tradable_markets=tradable_markets, provision=provision, initial_balance=starting_balance, leverage=leverage, reward_scaling=reward_scaling)

        observation = env.reset()
        done = False
        cumulative_reward = 0
        start_time = time.time()
        initial_balance = env.balance

        while not done:
            current_position = env.current_position
            action, prob, val = agent.choose_action(observation, current_position)
            static_input = current_position
            observation_, reward, done, info = env.step(action)
            agent.store_transition(observation, action, prob, val, reward, done, static_input)
            observation = observation_
            cumulative_reward += reward

            # Check if enough data is collected or if the dataset ends
            if len(agent.memory.states) >= agent.memory.batch_size:  # or done:
                agent.learn()
                agent.memory.clear_memory()

        # results
        end_time = time.time()
        episode_time = end_time - start_time
        total_rewards.append(cumulative_reward)
        episode_durations.append(episode_time)
        total_balances.append(env.balance)

        print(f"\nCompleted learning from randomly selected window in episode {episode + 1}: Total Reward: {cumulative_reward}, Total Balance: {env.balance:.2f}, Duration: {episode_time:.2f} seconds")
        print("-----------------------------------")

    print('end')
