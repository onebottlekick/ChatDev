import importlib
import json
import logging
import os
import shutil
import time
from datetime import datetime

from camel.agents import RolePlaying
from camel.configs import ChatGPTConfig
from camel.typing import TaskType, ModelType
from chatdev.chat_env import ChatEnv, ChatEnvConfig
from chatdev.statistics import get_info
from camel.web_spider import modal_trans
from chatdev.utils import now


def check_bool(s):
    return s.lower() == "true"


class ChatChain:

    def __init__(self,
                 model_name: str,
                 user_token: str = None,
                 base_url:str = None,
                 config_path: str = None,
                 config_phase_path: str = None,
                 config_role_path: str = None,
                 task_prompt: str = None,
                 project_name: str = None,
                 org_name: str = None,
                 model_type: ModelType = ModelType.GPT_3_5_TURBO,
                 code_path: str = None) -> None:
        """

        Args:
            config_path: path to the ChatChainConfig.json
            config_phase_path: path to the PhaseConfig.json
            config_role_path: path to the RoleConfig.json
            task_prompt: the user input prompt for software
            project_name: the user input name for software
            org_name: the organization name of the human user
        """

        # load config file
        self.model_name = model_name
        self.user_token = user_token
        self.base_url = base_url
        self.config_path = config_path
        self.config_phase_path = config_phase_path
        self.config_role_path = config_role_path
        self.project_name = project_name
        self.org_name = org_name
        self.model_type = model_type
        self.code_path = code_path

        with open(self.config_path, 'r', encoding="utf8") as file:
            self.config = json.load(file)
        with open(self.config_phase_path, 'r', encoding="utf8") as file:
            self.config_phase = json.load(file)
        with open(self.config_role_path, 'r', encoding="utf8") as file:
            self.config_role = json.load(file)

        # init chatchain config and recruitments
        self.chain = self.config["chain"]
        self.recruitments = self.config["recruitments"]
        self.web_spider = self.config["web_spider"]
        
        if "target_email_address" in self.config.keys():
            self.target_email_address = self.config["target_email_address"]
        else:
            self.config["target_email_address"] = None
            self.target_email_address = self.config["target_email_address"]

        # init default max chat turn
        self.chat_turn_limit_default = 10

        # init ChatEnv
        self.chat_env_config = ChatEnvConfig(clear_structure=check_bool(self.config["clear_structure"]),
                                             gui_design=check_bool(self.config["gui_design"]),
                                             git_management=check_bool(self.config["git_management"]),
                                             incremental_develop=check_bool(self.config["incremental_develop"]),
                                             background_prompt=self.config["background_prompt"],
                                             with_memory=check_bool(self.config["with_memory"]),
                                             target_email_address=self.config["target_email_address"])
                                             
        self.chat_env = ChatEnv(self.chat_env_config)

        # the user input prompt will be self-improved (if set "self_improve": "True" in ChatChainConfig.json)
        # the self-improvement is done in self.preprocess
        self.task_prompt_raw = task_prompt
        self.task_prompt = ""

        # init role prompts
        self.role_prompts = dict()
        for role in self.config_role:
            self.role_prompts[role] = "\n".join(self.config_role[role])

        # init log
        self.start_time, self.log_filepath = self.get_logfilepath()

        # init SimplePhase instances
        # import all used phases in PhaseConfig.json from chatdev.phase
        # note that in PhaseConfig.json there only exist SimplePhases
        # ComposedPhases are defined in ChatChainConfig.json and will be imported in self.execute_step
        self.compose_phase_module = importlib.import_module("chatdev.composed_phase")
        self.phase_module = importlib.import_module("chatdev.phase")
        self.phases = dict()
        for phase in self.config_phase:
            assistant_role_name = self.config_phase[phase]['assistant_role_name']
            user_role_name = self.config_phase[phase]['user_role_name']
            phase_prompt = "\n\n".join(self.config_phase[phase]['phase_prompt'])
            phase_class = getattr(self.phase_module, phase)
            phase_instance = phase_class(assistant_role_name=assistant_role_name,
                                         user_role_name=user_role_name,
                                         phase_prompt=phase_prompt,
                                         role_prompts=self.role_prompts,
                                         phase_name=phase,
                                         model_type=self.model_type,
                                         log_filepath=self.log_filepath,
                                         target_email_address=self.target_email_address,
                                         model_name=self.model_name,
                                         base_url=self.base_url)
            self.phases[phase] = phase_instance

    def make_recruitment(self):
        """
        recruit all employees
        Returns: None

        """
        for employee in self.recruitments:
            self.chat_env.recruit(agent_name=employee)

    def execute_step(self, phase_item: dict):
        """
        execute single phase in the chain
        Args:
            phase_item: single phase configuration in the ChatChainConfig.json

        Returns:

        """

        phase = phase_item['phase']
        phase_type = phase_item['phaseType']
        # For SimplePhase, just look it up from self.phases and conduct the "Phase.execute" method
        if phase_type == "SimplePhase":
            max_turn_step = phase_item['max_turn_step']
            need_reflect = check_bool(phase_item['need_reflect'])
            if phase in self.phases:
                self.chat_env = self.phases[phase].execute(self.chat_env,
                                                           self.chat_turn_limit_default if max_turn_step <= 0 else max_turn_step,
                                                           need_reflect)
            else:
                raise RuntimeError(f"Phase '{phase}' is not yet implemented in chatdev.phase")
        # For ComposedPhase, we create instance here then conduct the "ComposedPhase.execute" method
        elif phase_type == "ComposedPhase":
            cycle_num = phase_item['cycleNum']
            composition = phase_item['Composition']
            compose_phase_class = getattr(self.compose_phase_module, phase)
            if not compose_phase_class:
                raise RuntimeError(f"Phase '{phase}' is not yet implemented in chatdev.compose_phase")
            compose_phase_instance = compose_phase_class(phase_name=phase,
                                                         cycle_num=cycle_num,
                                                         composition=composition,
                                                         config_phase=self.config_phase,
                                                         config_role=self.config_role,
                                                         model_type=self.model_type,
                                                         log_filepath=self.log_filepath,
                                                         model_name=self.model_name,
                                                         base_url=self.base_url)
            self.chat_env = compose_phase_instance.execute(self.chat_env)
        else:
            raise RuntimeError(f"PhaseType '{phase_type}' is not yet implemented.")

    def execute_chain(self):
        """
        execute the whole chain based on ChatChainConfig.json
        Returns: None

        """
        for phase_item in self.chain:
            self.execute_step(phase_item)

    def get_logfilepath(self):
        """
        get the log path (under the software path)
        Returns:
            start_time: time for starting making the software
            log_filepath: path to the log

        """
        start_time = now()
        filepath = os.path.dirname(__file__)
        # root = "/".join(filepath.split("/")[:-1])
        root = os.path.dirname(filepath)
        # directory = root + "/WareHouse/"
        directory = os.path.join(root, "WareHouse", self.user_token, self.project_name, 'logs')
        os.makedirs(directory, exist_ok=True)
        log_filepath = os.path.join(directory,
                                    "{}.log".format("_".join([self.project_name, self.org_name, start_time])))
        return start_time, log_filepath

    def pre_processing(self):
        """
        remove useless files and log some global config settings
        Returns: None

        """
        filepath = os.path.dirname(__file__)
        root = os.path.dirname(filepath)
        if self.user_token is not None:
            directory = os.path.join(root, "WareHouse", self.user_token)
            os.makedirs(directory, exist_ok=True)
        else:
            directory = os.path.join(root, 'WareHouse')

        if self.chat_env.config.clear_structure:
            for filename in os.listdir(directory):
                file_path = os.path.join(directory, filename)
                # logs with error trials are left in WareHouse/
                if os.path.isfile(file_path) and not filename.endswith(".py") and not filename.endswith(".log"):
                    os.remove(file_path)

        software_path = os.path.join(directory, self.project_name)
        config_copy_path = os.path.join(software_path, "configs")
        os.makedirs(config_copy_path, exist_ok=True)
        self.chat_env.set_directory(software_path)

        if self.chat_env.config.with_memory is True:
            self.chat_env.init_memory()

        # copy config files to software path
        shutil.copy(self.config_path, config_copy_path)
        shutil.copy(self.config_phase_path, config_copy_path)
        shutil.copy(self.config_role_path, config_copy_path)

        # copy code files to software path in incremental_develop mode
        if check_bool(self.config["incremental_develop"]):
            for root, dirs, files in os.walk(self.code_path):
                relative_path = os.path.relpath(root, self.code_path)
                target_dir = os.path.join(software_path, 'base', relative_path)
                os.makedirs(target_dir, exist_ok=True)
                for file in files:
                    source_file = os.path.join(root, file)
                    target_file = os.path.join(target_dir, file)
                    shutil.copy2(source_file, target_file)
            self.chat_env._load_from_hardware(os.path.join(software_path, 'base'))

        # write task prompt to software
        with open(os.path.join(software_path, self.project_name + ".prompt"), "w") as f:
            f.write(self.task_prompt_raw)

        preprocess_msg = "**[Preprocessing]**\n\n"
        chat_gpt_config = ChatGPTConfig()

        preprocess_msg += "**ChatDev Starts** ({})\n\n".format(self.start_time)
        preprocess_msg += "**Timestamp**: {}\n\n".format(self.start_time)
        preprocess_msg += "**config_path**: {}\n\n".format(self.config_path)
        preprocess_msg += "**config_phase_path**: {}\n\n".format(self.config_phase_path)
        preprocess_msg += "**config_role_path**: {}\n\n".format(self.config_role_path)
        preprocess_msg += "**task_prompt**: {}\n\n".format(self.task_prompt_raw)
        preprocess_msg += "**project_name**: {}\n\n".format(self.project_name)
        preprocess_msg += "**Log File**: {}\n\n".format(self.log_filepath)
        preprocess_msg += "**ChatDevConfig**:\n{}\n\n".format(self.chat_env.config.__str__())
        preprocess_msg += "**ChatGPTConfig**:\n{}\n\n".format(chat_gpt_config)

        # init task prompt
        if check_bool(self.config['self_improve']):
            self.chat_env.env_dict['task_prompt'] = self.self_task_improve(self.task_prompt_raw)
        else:
            self.chat_env.env_dict['task_prompt'] = self.task_prompt_raw
        if(check_bool(self.web_spider)):
            self.chat_env.env_dict['task_description'] = modal_trans(self.task_prompt_raw)

    def post_processing(self):
        """
        summarize the production and move log files to the software directory
        Returns: None

        """

        self.chat_env.write_meta()
        filepath = os.path.dirname(__file__)
        root = os.path.dirname(filepath)

        if self.chat_env_config.git_management:
            self.chat_env.codes.version += 1
            os.system("cd {}; git add .".format(self.chat_env.env_dict["directory"]))
            os.system("cd {}; git commit -m \"v{} Final Version\"".format(self.chat_env.env_dict["directory"],
                                                                          self.chat_env.codes.version))
            
            import subprocess
            # execute git log
            command = "cd {}; git log".format(self.chat_env.env_dict["directory"])
            completed_process = subprocess.run(command, shell=True, text=True, stdout=subprocess.PIPE)

        directory = self.chat_env.env_dict['directory']
        if self.chat_env.config.clear_structure:
            for filename in os.listdir(directory):
                file_path = os.path.join(directory, filename)
                if os.path.isdir(file_path) and file_path.endswith("__pycache__"):
                    shutil.rmtree(file_path, ignore_errors=True)

        time.sleep(1)

    # @staticmethod
    def self_task_improve(self, task_prompt):
        """
        ask agent to improve the user query prompt
        Args:
            task_prompt: original user query prompt

        Returns:
            revised_task_prompt: revised prompt from the prompt engineer agent

        """
        self_task_improve_prompt = """I will give you a short description of a software design requirement, 
please rewrite it into a detailed prompt that can make large language model know how to make this software better based this prompt,
the prompt should ensure LLMs build a software that can be run correctly, which is the most import part you need to consider.
remember that the revised prompt should not contain more than 200 words, 
here is the short description:\"{}\". 
If the revised prompt is revised_version_of_the_description, 
then you should return a message in a format like \"<INFO> revised_version_of_the_description\", do not return messages in other formats.""".format(
            task_prompt)
        role_play_session = RolePlaying(
            assistant_role_name="Prompt Engineer",
            assistant_role_prompt="You are an professional prompt engineer that can improve user input prompt to make LLM better understand these prompts.",
            user_role_prompt="You are an user that want to use LLM to build software.",
            user_role_name="User",
            task_type=TaskType.CHATDEV,
            task_prompt="Do prompt engineering on user query",
            with_task_specify=False,
            model_type=self.model_type,
            base_url=self.base_url
        )

        _, input_user_msg = role_play_session.init_chat(None, None, self_task_improve_prompt)
        assistant_response, user_response = role_play_session.step(input_user_msg, True)
        revised_task_prompt = assistant_response.msg.content.split("<INFO>")[-1].lower().strip()
        return revised_task_prompt
