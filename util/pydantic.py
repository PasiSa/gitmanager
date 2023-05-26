from typing import TYPE_CHECKING, Any, Dict, Generator, List, Optional, Sequence, Tuple, Type, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.error_wrappers import ErrorWrapper, ValidationError, display_errors, get_exc_type
from pydantic.fields import Field, PrivateAttr
from pydantic.main import BaseConfig
# pydantic doesn't export the metaclass, so we get it here
ModelMetaclass = type(BaseModel)

if TYPE_CHECKING:
    from pydantic.error_wrappers import Loc


class UndefinedError(TypeError): ...


class UndefinedType:
    # make the objects immutable, so the Undefined global isn't changed
    __slots__ = ()

    def __new__(cls) -> "UndefinedType":
        return Undefined

    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v: Any) -> "UndefinedType":
        if not isinstance(v, cls):
            raise UndefinedError("'Undefined' is for internal use only")
        return v

    def __bool__(self):
        return False

Undefined = object.__new__(UndefinedType)


# default_factory is required, so that pydantic doesn't make a copy of the
# Undefined object which would break "X is Undefined" checks
undefined_field = Field(default_factory=lambda: Undefined)

class PydanticModelMeta(ModelMetaclass):
    """
    Pydantic ModelMetaclass but adds Undefined functionality:
    - Undefined type fields are automatically defaulted to Undefined()
    """
    def __new__(
            cls,
            name: str,
            bases: Tuple[type, ...],
            namespace: Dict[str, Any],
            **kwargs: Any
            ) -> "PydanticModelMeta":
        print(f"Pydantic weirdness -- cls: {cls}, name: {name}")
        if "__annotations__" in namespace:
            for attr, type in namespace["__annotations__"].items():
                print(f"Pydanting annotations -- attr: {attr}, type: {type}")
                if attr in namespace:
                    continue
                if type == UndefinedType:
                    namespace[attr] = undefined_field
                elif get_origin(type) == Union:
                    if UndefinedType in get_args(type):
                        namespace[attr] = undefined_field
                        print(f"Pydantic namespace: {namespace[attr]}")

        return super().__new__(cls, name, bases, namespace, **kwargs) # type: ignore


class PydanticModel(BaseModel, metaclass=PydanticModelMeta):
    """
    Pydantic BaseModel but adds Undefined and warning functionality:
    - Undefined type fields are automatically defaulted to Undefined()
    - Adds exclude_undefined to .dict(...) to exclude undefined fields
    - Adds add_warning and get_warnings_nested for setting and getting warnings
    """
    _warnings: Dict[str, List[str]] = PrivateAttr(default={})

    def dict(self, *, exclude_undefined: bool = True, **kwargs: Any) -> Dict[str, Any]:
        out = super().dict(**kwargs)
        if exclude_undefined:
            for k, v in list(out.items()):
                if isinstance(v, UndefinedType):
                    del out[k]
        return out

    def add_warning(self, msg: str, key: str = "__root__") -> None:
        self._warnings.setdefault(key, []).append(msg)

    def get_warnings_nested(self, prefix: str = "") -> Dict[str, List[str]]:
        warnings = {f"{prefix}.{k}": v for k,v in self._warnings.items() if k != "__root__"}
        if "__root__" in self._warnings:
            warnings[prefix] = self._warnings["__root__"]
        for k,v in self:
            warnings.update(get_all_warnings(v, prefix, k))
        return warnings


def add_warnings_to_values_dict(dict: Dict[str, Any], key: str, msg: str) -> None:
    dict.setdefault("_warnings", {}).setdefault(key, []).append(msg)


def get_all_warnings(value: Any, prefix: str = "", key: str = "") -> Dict[str, List[str]]:
    if isinstance(value, PydanticModel):
        return value.get_warnings_nested(prefix + key)
    elif isinstance(value, list):
        warnings: Dict[str, List[str]] = {}
        for i, v in enumerate(value):
            warnings.update(get_all_warnings(v, prefix, f"{key}[{i}]"))
        return warnings
    elif isinstance(value, dict):
        warnings: Dict[str, List[str]] = {}
        for k, v in value.items():
            warnings.update(get_all_warnings(v, prefix, f"{key}['{k}']"))
        return warnings
    else:
        return {}


_ObjT = TypeVar("_ObjT")
NotRequired = Union[_ObjT, UndefinedType]

# slightly modified versions of the pydantic functions
# they add model type information to the error dicts and skips UndefinedErrors
def error_dict(exc: Exception, config: Type[BaseConfig], models: List[type], loc: 'Loc') -> Dict[str, Any]:
    type_ = get_exc_type(exc.__class__)
    msg_template = config.error_msg_templates.get(type_) or getattr(exc, 'msg_template', None)
    ctx = exc.__dict__
    if msg_template:
        msg = msg_template.format(**ctx)
    else:
        msg = str(exc)

    d: Dict[str, Any] = {'loc': loc, 'msg': msg, 'type': type_, 'models': models}

    if ctx:
        d['ctx'] = ctx

    return d


def flatten_errors(
        errors: Sequence[Any],
        config: Type[BaseConfig],
        models: List[type],
        loc: Optional['Loc'] = None,
        ) -> Generator[Dict[str, Any], None, None]:
    for error in errors:
        if isinstance(error, ErrorWrapper):
            if loc:
                error_loc = loc + error.loc_tuple()
            else:
                error_loc = error.loc_tuple()

            if isinstance(error.exc, ValidationError):
                yield from flatten_errors(error.exc.raw_errors, config, models + [error.exc.model], error_loc)
            elif isinstance(error.exc, UndefinedError):
                # we dont want to show UndefinedError to the user
                # as it cannot be loaded from a dict
                pass
            else:
                yield error_dict(error.exc, config, models, error_loc)
        elif isinstance(error, list):
            yield from flatten_errors(error, config, models, loc=loc)
        else:
            raise RuntimeError(f'Unknown error object: {error}')


def validation_error_str(e: ValidationError) -> str:
    """
    slightly modified versions of pydantic's ValidationError.__str__
    adds model type information to the output
    """
    try:
        config = e.model.__config__  # type: ignore
    except AttributeError:
        config = e.model.__pydantic_model__.__config__  # type: ignore

    last_models = None
    errors = list(flatten_errors(e.raw_errors, config, []))
    num_errors = len(errors)
    out = f'{num_errors} validation error{"" if num_errors == 1 else "s"} for {e.model.__name__}'
    for error in errors:
        if last_models != error["models"]:
            last_models = error["models"]
            if len(last_models) != 0:
                if len(error["loc"]) != 0:
                    out += "\n" + " -> ".join((str(i) for i in error["loc"][:-1])) + f" cannot be {last_models[-1].__name__} because"
                else:
                    out += f"\n Cannot be {last_models[-1].__name__} because"
        # add indent to the error string
        out += "\n  " + "\n  ".join(display_errors([error]).split("\n"))
    return out


def validation_warning_str(obj: Any) -> str:
    warnings = get_all_warnings(obj)
    if warnings:
        num_errors = len(warnings)
        out = f'{num_errors} validation warning{"" if num_errors == 1 else "s"} for {obj.__class__.__name__}'
        for loc,warnings in warnings.items():
            for warning in warnings:
                out += f"\n{loc}: {warning}"
        return out
    else:
        return ""
