# TODO: we should have more advanced mechanism to handle such requirements for saving sessions.
import json
from pathlib import Path
from typing import Any, Tuple

import fire
from jinja2 import Environment, StrictUndefined

from rdagent.app.qlib_rd_loop.conf import FactorBasePropSetting
from rdagent.components.document_reader.document_reader import (
    extract_first_page_screenshot_from_pdf,
    load_and_process_pdfs_by_langchain,
)
from rdagent.components.workflow.conf import BasePropSetting
from rdagent.components.workflow.rd_loop import RDLoop
from rdagent.core.developer import Developer
from rdagent.core.exception import FactorEmptyError
from rdagent.core.prompts import Prompts
from rdagent.core.proposal import Hypothesis, HypothesisExperiment2Feedback, Trace
from rdagent.core.scenario import Scenario
from rdagent.core.utils import import_class
from rdagent.log import rdagent_logger as logger
from rdagent.oai.llm_utils import APIBackend
from rdagent.scenarios.qlib.experiment.factor_experiment import QlibFactorExperiment
from rdagent.scenarios.qlib.experiment.factor_from_report_experiment import (
    QlibFactorFromReportScenario,
)
from rdagent.scenarios.qlib.factor_experiment_loader.pdf_loader import (
    FactorExperimentLoaderFromPDFfiles,
)
from rdagent.utils.workflow import LoopBase, LoopMeta

prompts_path = Path(__file__).parent / "prompts.yaml"
prompts = Prompts(file_path=prompts_path)


def generate_hypothesis(factor_result: dict, report_content: str) -> str:
    system_prompt = (
        Environment(undefined=StrictUndefined).from_string(prompts["hypothesis_generation"]["system"]).render()
    )
    user_prompt = (
        Environment(undefined=StrictUndefined)
        .from_string(prompts["hypothesis_generation"]["user"])
        .render(factor_descriptions=json.dumps(factor_result), report_content=report_content)
    )

    response = APIBackend().build_messages_and_create_chat_completion(
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        json_mode=True,
    )

    response_json = json.loads(response)

    return Hypothesis(
        hypothesis=response_json.get("hypothesis", "No hypothesis provided"),
        reason=response_json.get("reason", "No reason provided"),
        concise_reason=response_json.get("concise_reason", "No concise reason provided"),
        concise_observation=response_json.get("concise_observation", "No concise observation provided"),
        concise_justification=response_json.get("concise_justification", "No concise justification provided"),
        concise_knowledge=response_json.get("concise_knowledge", "No concise knowledge provided"),
    )


def extract_hypothesis_and_exp_from_reports(report_file_path: str) -> Tuple[QlibFactorExperiment, Hypothesis]:
    with logger.tag("extract_factors_and_implement"):
        with logger.tag("load_factor_tasks"):
            exp = FactorExperimentLoaderFromPDFfiles().load(report_file_path)
            if exp is None or exp.sub_tasks == []:
                return None, None

        with logger.tag("load_pdf_screenshot"):
            pdf_screenshot = extract_first_page_screenshot_from_pdf(report_file_path)
            logger.log_object(pdf_screenshot)

    docs_dict = load_and_process_pdfs_by_langchain(Path(report_file_path))

    factor_result = {
        task.factor_name: {
            "description": task.factor_description,
            "formulation": task.factor_formulation,
            "variables": task.variables,
            "resources": task.factor_resources,
        }
        for task in exp.sub_tasks
    }

    report_content = "\n".join(docs_dict.values())
    hypothesis = generate_hypothesis(factor_result, report_content)
    return exp, hypothesis


class FactorReportLoop(RDLoop, metaclass=LoopMeta):
    skip_loop_error = (FactorEmptyError,)

    def __init__(self, PROP_SETTING: FactorBasePropSetting):
        self.judge_pdf_data_items = json.load(open(PROP_SETTING.report_result_json_file_path, "r"))
        self.pdf_file_index = 0
        self.valid_pdf_file_count = 0
        super().__init__(PROP_SETTING=PROP_SETTING)

    def propose_hypo_exp(self, prev_out: dict[str, Any]):
        with logger.tag("r"):
            while True:
                if self.valid_pdf_file_count > 15:
                    break
                report_file_path = self.judge_pdf_data_items[self.pdf_file_index]
                logger.info(f"Processing number {self.pdf_file_index} report: {report_file_path}")
                self.pdf_file_index += 1
                exp, hypothesis = extract_hypothesis_and_exp_from_reports(str(report_file_path))
                if exp is None:
                    continue
                self.valid_pdf_file_count += 1
                exp.based_experiments = [QlibFactorExperiment(sub_tasks=[])] + [t[1] for t in self.trace.hist if t[2]]
                exp.sub_workspace_list = exp.sub_workspace_list[: FACTOR_FROM_REPORT_PROP_SETTING.max_factor_per_report]
                exp.sub_tasks = exp.sub_tasks[: FACTOR_FROM_REPORT_PROP_SETTING.max_factor_per_report]
                logger.log_object(hypothesis, tag="hypothesis generation")
                logger.log_object(exp.sub_tasks, tag="experiment generation")
                self.current_loop_hypothesis = hypothesis
                self.current_loop_exp = exp
                return None

    def propose(self, prev_out: dict[str, Any]):
        return self.current_loop_hypothesis

    def exp_gen(self, prev_out: dict[str, Any]):
        return self.current_loop_exp


def main(path=None, step_n=None):
    """
    You can continue running session by

    .. code-block:: python

        dotenv run -- python rdagent/app/qlib_rd_loop/factor_from_report_sh.py $LOG_PATH/__session__/1/0_propose  --step_n 1   # `step_n` is a optional parameter

    """
    if path is None:
        model_loop = FactorReportLoop(FACTOR_FROM_REPORT_PROP_SETTING)
    else:
        model_loop = FactorReportLoop.load(path)
    model_loop.run(step_n=step_n)


if __name__ == "__main__":
    fire.Fire(main)