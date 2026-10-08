"""
配置加载：把 config/ 下的 yaml 读成字典

约定：
- 每个 yml 一个 loader，路径用 get_abs_path 锚定项目根（不受启动目录影响）；
- 模块底部集中实例化，供各模块 `from utils.config_handler import xxx_conf` 使用。
  注意这几个 dict 是**按引用共享**的，运行时改它们会全局生效。
  ⚠️ 这也意味着：改 `chroma_conf` 会影响**所有** VectorStoreService ——
  eval.retrieval_eval 的 matrix 模式靠它临时切换参数，但**普通评估路径
  直接对生产库调用 load_document()**，参数若与生产不一致会触发生产库重建。
  评估后务必把改动还原（matrix 模式已在 finally 里还原）。
"""
import yaml
from utils.path_tool import get_abs_path


def load_model_config(config_path: str=get_abs_path("config/model.yml"), encoding: str="utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, Loader=yaml.FullLoader)


def load_chroma_config(config_path: str=get_abs_path("config/chroma.yml"), encoding: str="utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, Loader=yaml.FullLoader)


def load_prompts_config(config_path: str=get_abs_path("config/prompts.yml"), encoding: str="utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, Loader=yaml.FullLoader)


def load_agent_config(config_path: str=get_abs_path("config/agent.yml"), encoding: str="utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, Loader=yaml.FullLoader)


model_conf = load_model_config()
chroma_conf = load_chroma_config()
prompts_conf = load_prompts_config()
agent_conf = load_agent_config()


if __name__ == '__main__':
    print(f"chat_model_name      = {model_conf['chat_model_name']}")
    print(f"embedding_model_name = {model_conf['embedding_model_name']}")
