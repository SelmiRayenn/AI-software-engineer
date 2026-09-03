from sqlalchemy.orm import Session

from app.models import GoldPatch


class GoldSolutionStorage:
    def create_gold_patch(
        self,
        db: Session,
        benchmark_task_id,
        changed_files: list[str],
        patch_text: str,
        test_files: list[str],
    ) -> GoldPatch:
        gold_patch = GoldPatch(
            benchmark_task_id=benchmark_task_id,
            changed_files=changed_files,
            patch_text=patch_text,
            test_files=test_files,
        )
        db.add(gold_patch)
        db.flush()
        return gold_patch
