from typing import Any, Callable, Mapping, Tuple

import contextvars

from instructor.retry import InstructorRetryException

from parea.utils.trace_utils import trace_insert

from parea.helpers import gen_trace_id
from wrapt import wrap_object

from parea import trace
from parea.schemas import EvaluationResult
from parea.utils.trace_integrations.wrapt_utils import CopyableFunctionWrapper

instructor_trace_id = contextvars.ContextVar("instructor_trace_id", default='')
instructor_val_err_count = contextvars.ContextVar("instructor_val_err_count", default=0)
instructor_val_errs = contextvars.ContextVar("instructor_val_errs", default=[])


def instrument_instructor_validation_errors() -> None:
    for retry_method in ['retry_async', 'retry_sync']:
        wrap_object(
            module='instructor.patch',
            name=f"{retry_method}",
            factory=CopyableFunctionWrapper,
            args=(_RetryWrapper(),),
        )

    wrap_object(
        module='tenacity',
        name="AttemptManager.__exit__",
        factory=CopyableFunctionWrapper,
        args=(_AttemptManagerExitWrapper(),),
    )


class _RetryWrapper:
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[type, Any],
        kwargs: Mapping[str, Any],
    ) -> Any:
        trace_id = gen_trace_id()
        instructor_trace_id.set(trace_id)
        return trace(name='instructor', _trace_id=trace_id)(wrapped)(*args, **kwargs)


class _AttemptManagerExitWrapper:
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[type, Any],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if instructor_trace_id.get() is not None:
            if len(args) > 1 and args[1] is not None and isinstance(args[1], InstructorRetryException):
                instructor_val_err_count.set(instructor_val_err_count.get() + 1)
                reasons = []
                for arg in args[1].args:
                    reasons.append(str(arg))
                instructor_val_errs.get().extend(reasons)
            else:
                reason = '\n\n\n'.join(instructor_val_errs.get())
                instructor_score = EvaluationResult(
                    name='instruction_validation_error_count',
                    score=instructor_val_err_count.get(),
                    reason=reason,
                )
                trace_insert({'scores': [instructor_score]}, instructor_trace_id.get())
                instructor_trace_id.set('')
                instructor_val_err_count.set(0)
                instructor_val_errs.set([])
        return wrapped(*args, **kwargs)
