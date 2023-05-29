import logging
from pathlib import Path
from typing import Any, Dict, Generator, List, Literal, Optional, Set, Tuple, Type, TypeVar, Union
from datetime import date, datetime, timedelta
import os
import time

from django.conf import settings
from pydantic import AnyHttpUrl, Field, BaseModel
from pydantic.class_validators import root_validator, validator
from pydantic.fields import PrivateAttr
from pydantic.types import NonNegativeInt, PositiveInt, confloat

from util.files import is_subpath
from util.localize import Localized, DEFAULT_LANG
from util.pydantic import PydanticModel, NotRequired, Undefined, add_warnings_to_values_dict
from util.static import static_url
from .parser import ConfigParser


LOGGER = logging.getLogger('main')


def _get_datetime(value: Any) -> Optional[datetime]:
    """Turns date/datetime into a datetime and returns None if given anything else"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.max.time())
    return None


class SimpleDuration(PydanticModel):
    __root__: str

    @root_validator(allow_reuse=True)
    def simple_duration(cls, values: dict):
        delta = values.get('__root__')
        if not isinstance(delta, str):
            raise ValueError("A duration must be a string")
        if not delta:
            raise ValueError("An empty string cannot be turned into a duration")

        try:
            int(delta[:-1])
        except:
            raise ValueError("Format: <integer>(y|m|d|h|w) e.g. 3d")

        if delta[-1] in ("y", "m", "w", "d", "h"):
            return values
        else:
            raise ValueError("Format: <integer>(y|m|d|h|w) e.g. 3d")

AnyDuration = Union[timedelta, SimpleDuration]
AnyDate = Union[datetime, date, str]
# FIXME: str in AnyDate passes invalid dates and any strings.
# str should be removed. However, removing it causes crashes with the Undefined type.


Float0to1 = confloat(ge=0, le=1)


class ConfigureOptions(PydanticModel):
    files: Dict[str,str] = {}
    # ellipsis (...) makes the field required in the case that a default url isn't specified
    url: str = settings.DEFAULT_GRADER_URL or ... # type: ignore


class RevealRuleOptions(BaseModel):
    trigger: Literal["immediate", "manual", "time", "deadline", "deadline_all", "completion"]
    time: NotRequired[AnyDate]
    delay_minutes: NotRequired[NonNegativeInt]
    #time: date
    #delay_minutes: int


class ExerciseConfig(PydanticModel):
    data: Dict[str, dict]
    file: str
    mtime: float
    ptime: float
    default_lang: str

    def data_for_language(self, lang: Optional[str] = None) -> dict:
        if lang == '_root':
            return self.data

        # Try to find version for requested or configured language.
        for lang in (lang, self.default_lang):
            if lang in self.data:
                data = self.data[lang]
                data["lang"] = lang
                return data

        # Fallback to any existing language version.
        return list(self.data.values())[0]

    @staticmethod
    def load(exercise_key: str, course_dir: str, filename: str, lang: str) -> "ExerciseConfig":
        '''
        Default loader to find and parse file.

        @type course_root: C{dict}
        @param course_root: a course root dictionary
        @type exercise_key: C{str}
        @param exercise_key: an exercise key
        @type filename: C{str}
        @param filename: config file name
        @type course_dir: C{str}
        @param course_dir: a path to the course root directory
        @rtype: C{str}, C{dict}
        @return: exercise config file path, modified time and data dict
        '''
        config_file = ConfigParser.get_config(os.path.join(course_dir, filename))
        mtime, data = ConfigParser.parse(config_file)
        if "include" in data:
            include_file_timestamp, data = ConfigParser._include(data, config_file, course_dir)

            # Save the latest modification time of the exercise in the cache.
            # If there is an included base template, its modification time may be later.
            mtime = max(mtime, include_file_timestamp)

        # Process key modifiers and create language versions of the data.
        data = ConfigParser.process_tags(data, lang)
        for version in data.values():
            ConfigParser.check_fields(config_file, version, ["title", "view_type"])
            version["key"] = exercise_key
            version["mtime"] = mtime

        return ExerciseConfig.parse_obj({
            "file": config_file,
            "mtime": mtime,
            "ptime": time.time(),
            "data": data,
            "default_lang": lang,
        })


class Parent(PydanticModel):
    children: List[Union["Chapter", "Exercise", "LTIExercise", "ExerciseCollection"]] = []

    def postprocess(self, **kwargs: Any):
        for c in self.children:
            c.postprocess(**kwargs)

    def child_categories(self) -> Set[str]:
        """Returns a set of categories of children recursively"""
        categories: Set[str] = set()
        for c in self.children:
            categories.add(c.category)
            categories.union(c.child_categories())
        return categories

    def child_keys(self) -> List[str]:
        """Returns a list of keys of children recursively"""
        keys: List[str] = []
        for c in self.children:
            keys.append(c.key)
            keys.extend(c.child_keys())
        return keys

    _ClsT = TypeVar("_ClsT", bound="Parent")
    def gather_types(self, clss: Type[_ClsT]) -> Generator[_ClsT, None, None]:
        """
        Recursively yields all the children of particular type.
        """
        if isinstance(self, clss):
            yield self

        for c in self.children:
            yield from c.gather_types(clss)


class Item(Parent):
    key: str
    category: str
    status: NotRequired[str]
    order: NotRequired[int]
    audience: NotRequired[Literal["internal", "external", "registered"]]
    name: NotRequired[Localized[str]]
    description: NotRequired[str]
    use_wide_column: NotRequired[bool]
    url: NotRequired[Localized[str]] # TODO: url check
    model_answer: NotRequired[Localized[str]]  # TODO: url check
    exercise_template: NotRequired[Localized[str]] # TODO: url check
    exercise_info: NotRequired[Any] # TODO: json check

    class Config:
        extra = "forbid"

    @root_validator(allow_reuse=True, pre=True)
    def name_or_title(cls, values: Dict[str, Any]):
        if "name" in values and "title" in values:
            raise ValueError("Only one of name and title should be specified")
        if "title" in values:
            values["name"] = values.pop("title")
        return values

    @root_validator(allow_reuse=True, pre=True)
    def remove_fields(cls, values: Dict[str, Any]):
        values = {k:v for k,v in values.items() if k[0] != "_"}
        # DEPRECATED: scale_points exists for some reason in some index.yaml
        # it isn't used anywhere though. It should be removed altogether
        values.pop("scale_points", None)
        return values


class Exercise(Item):
    max_submissions: NonNegativeInt = 0
    configure: NotRequired[ConfigureOptions]
    allow_assistant_viewing: NotRequired[bool]
    allow_assistant_grading: NotRequired[bool]
    config: NotRequired[Path]
    type: NotRequired[str]
    confirm_the_level: NotRequired[bool]
    difficulty: NotRequired[str]
    min_group_size: NotRequired[NonNegativeInt]
    max_group_size: NotRequired[NonNegativeInt]
    max_points: NotRequired[NonNegativeInt]
    points_to_pass: NotRequired[NonNegativeInt]
    reveal_submission_feedback: NotRequired[RevealRuleOptions]
    reveal_model_solutions: NotRequired[RevealRuleOptions]
    grading_mode: NotRequired[Literal["best", "last"]]
    _config_obj: Optional[ExerciseConfig] = PrivateAttr(default=None)

    def config_file_info(self, course_dir: str, grader_config_dir: str) -> Optional[Tuple[str, str]]:
        """
        Returns a tuple of the relative config file path and the absolute directory path, or None if
        config is Undefined.
        """
        if self.config is not Undefined:
            if self.config.is_absolute():
                return (
                    course_dir,
                    str(self.config)[1:],
                )
            else:
                return (
                    grader_config_dir,
                    str(self.config),
                )
        else:
            return None

    def postprocess(self, *, course_key: str, course_dir: str, grader_config_dir: str, default_lang: str, **kwargs: Any):
        super().postprocess(
            course_key = course_key,
            course_dir=course_dir,
            grader_config_dir=grader_config_dir,
            default_lang=default_lang,
            **kwargs,
        )

        LOGGER.debug('Loading exercise "%s/%s"', course_dir, self.key)
        config_file_info = self.config_file_info(course_dir, grader_config_dir)
        if config_file_info:
            self._config_obj = ExerciseConfig.load(
                self.key,
                *config_file_info,
                default_lang,
            )

        # DEPRECATED: default configure settings
        # this is for backwards compatibility and should be removed in the future
        if not self.configure and self._config_obj and settings.DEFAULT_GRADER_URL is not None:
            files = {}
            for lang_data in self._config_obj.data.values():
                mount = lang_data.get("container", {}).get("mount")
                if mount:
                    files[mount] = mount

                template = lang_data.get("template")
                if template and template.startswith("./"):
                    files[template] = template

                template = lang_data.get("feedback_template")
                if template and template.startswith("./"):
                    files[template] = template

                file = lang_data.get("instructions_file")
                if file:
                    if not isinstance(file, str):
                        raise ValueError(f"instructions_file is not a string in {self.config}")
                    if file.startswith("./"):
                        files[file] = course_key + "/" + file[2:]
                    else:
                        files[file] = file

                view_type = lang_data.get("view_type")
                if view_type:
                    if not isinstance(view_type, str):
                        raise ValueError(f"view_type is not a string in {self.config}")

                    if view_type.startswith("."):
                        path = view_type[1:].rsplit(".", 1)[0]
                        path = path.replace(".", "/") + ".py"
                        parts = path.rsplit("/", 1)
                        # if __init__.py exists, it is probably a package so send the whole directory
                        if len(parts) == 2 and Path(course_dir, parts[0], "__init__.py").exists():
                            files[parts[0]] = parts[0]
                        else:
                            files[path] = path

            self.configure = ConfigureOptions.parse_obj({"files": files})


    @root_validator(allow_reuse=True, skip_on_failure=True)
    def validate_assistant_permissions(cls, values: Dict[str, Any]):
        if not values.get("allow_assistant_viewing", False) and values.get("allow_assistant_grading", True):
            add_warnings_to_values_dict(values, "_root_", "Assistant grading is allowed but viewing is not")
        return values


class LTIExercise(Exercise):
    lti: str
    lti_context_id: NotRequired[str]
    lti_resource_link_id: NotRequired[str]
    lti_aplus_get_and_post: NotRequired[bool]
    lti_open_in_iframe: NotRequired[bool]


class ExerciseCollection(Item):
    target_category: str
    target_url: str
    max_points: PositiveInt
    points_to_pass: NotRequired[NonNegativeInt]

    @root_validator(allow_reuse=True, skip_on_failure=True)
    def validate_assistant_permissions(cls, values: Dict[str, Any]):
        if values["target_category"] == values["category"]:
            raise ValueError("Exercise collection's category and target category cannot be the same")
        return values


class Chapter(Item):
    static_content: Localized[Path]
    generate_table_of_contents: NotRequired[bool]

    @validator('static_content', allow_reuse=True)
    def validate_static_content(cls, paths: Localized[Path]):
        for path in paths.values():
            if path.is_absolute():
                raise ValueError("Path must be relative")
        return paths

Parent.update_forward_refs()
Exercise.update_forward_refs()
LTIExercise.update_forward_refs()
ExerciseCollection.update_forward_refs()
Chapter.update_forward_refs()


class Module(Parent):
    name: Localized[str]
    key: str
    status: NotRequired[str]
    order: NotRequired[int]
    introduction: NotRequired[str]
    open: NotRequired[AnyDate]
    close: NotRequired[AnyDate]
    duration: NotRequired[AnyDuration]
    read_open: NotRequired[Optional[AnyDate]] = Field(alias="read-open")
    points_to_pass: NotRequired[NonNegativeInt]
    late_close: NotRequired[AnyDate]
    late_penalty: NotRequired[Float0to1]
    late_duration: NotRequired[AnyDuration]
    numerate_ignoring_modules: NotRequired[bool]

    @root_validator(allow_reuse=True, pre=True)
    def name_or_title(cls, values: Dict[str, Any]):
        if "name" in values and "title" in values:
            raise ValueError("Only one of name and title should be specified")
        if "title" in values:
            values["name"] = values.pop("title")
        return values

    @root_validator(allow_reuse=True, skip_on_failure=True)
    def validate_dates(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        close = values.get("close")
        close_dt = _get_datetime(close)
        
        open = values.get("open")
        open_dt = _get_datetime(open)
        if open_dt and close_dt and open_dt > close_dt:
            raise ValueError(f"Module 'close' ({close}) before 'open' ({open})")

        late_close = values.get("late_close")
        late_close_dt = _get_datetime(late_close)
        if late_close_dt and close_dt and late_close_dt < close_dt:
            raise ValueError(f"'late_close' ({late_close}) is before 'close' ({close})")

        read_open = values.get("read_open")
        read_open_dt = _get_datetime(read_open)
        if read_open_dt and open_dt and read_open_dt >= open_dt:
            raise ValueError(f"'read_open' ({read_open}) is not before 'open' ({open})")

        return values

NumberingType = Literal["none", "arabic", "roman", "hidden"]


class Course(PydanticModel):
    name: Localized[str]
    modules: List[Module]
    lang: Union[str, List[str]] = DEFAULT_LANG
    archive_time: NotRequired[AnyDate]
    assistants: NotRequired[List[str]]
    categories: Dict[str, Any] = {} # TODO: add a pydantic model for categories
    contact: NotRequired[str]
    content_numbering: NotRequired[NumberingType]
    course_description: NotRequired[str]
    course_footer: NotRequired[str]
    description: NotRequired[str]
    start: NotRequired[AnyDate]
    end: NotRequired[AnyDate]
    enrollment_audience: NotRequired[Literal["internal", "external", "all"]]
    enrollment_end: NotRequired[AnyDate]
    enrollment_start: NotRequired[AnyDate]
    head_urls: NotRequired[List[Union[AnyHttpUrl, Path]]]
    index_mode: NotRequired[Literal["results", "toc", "last", "experimental"]]
    lifesupport_time: NotRequired[AnyDate]
    module_numbering: NotRequired[NumberingType]
    numerate_ignoring_modules: NotRequired[bool]
    view_content_to: NotRequired[Literal["enrolled", "enrollment_audience", "all_registered", "public"]]
    static_dir: NotRequired[Path]
    unprotected_paths: NotRequired[Set[Path]]
    configures: List[ConfigureOptions] = []

    def postprocess(self, course_key: str, **kwargs: Any):
        for c in self.modules:
            c.postprocess(course_key=course_key, **kwargs)

        if self.head_urls is not Undefined:
            nurls: List[Union[AnyHttpUrl, Path]] = []
            for url in self.head_urls:
                if isinstance(url, Path):
                    if url.is_absolute():
                        url = url.relative_to("/")
                    httpurl = static_url(course_key, url)
                    if httpurl is None:
                        self.add_warning(f"settings.STATIC_CONTENT_HOST not set (by admin). Cannot determine host for {url}", "head_urls")
                        continue
                    nurls.append(httpurl)
                else:
                    nurls.append(url)
            self.head_urls = nurls

    def exercises(self) -> Generator[Exercise, None, None]:
        for m in self.modules:
            yield from m.gather_types(Exercise)

    @root_validator(allow_reuse=True, pre=True)
    def change_language_to_lang(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if "language" in values and "lang" not in values:
            values["lang"] = values.pop("language")
        return values

    @validator('modules', allow_reuse=True)
    def validate_module_keys(cls, modules: List[Module]) -> List[Module]:
        keys = set()
        for m in modules:
            if m.key in keys:
                raise ValueError(f"Duplicate module key: {m.key}")
            keys.add(m.key)
        return modules

    @validator('configures', allow_reuse=True)
    def validate_configures(cls, configures: List[ConfigureOptions]):
        urls = set()
        for c in configures:
            if c.url in urls:
                raise ValueError(f"Duplicate configure URL: {c.url}")
            urls.add(c.url)

        return configures

    @validator('unprotected_paths', allow_reuse=True)
    def validate_unprotected_paths(cls, paths: Set[Path]) -> Set[Path]:
        for path in paths:
            if path.is_absolute():
                raise ValueError("Unprotected paths must be relative to the static directory (i.e. they cannot start with /)")
            if not is_subpath(path):
                raise ValueError("Unprotected paths must be under the static directory (paths cannot navigate outside it with ../)")
        return paths.union(Path(p) for p in ("_downloads", "_static", "_images"))

    @root_validator(allow_reuse=True, skip_on_failure=True)
    def validate_categories(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        for m in values["modules"]:
            for c in m.child_categories():
                if c not in values["categories"]:
                    raise ValueError(f"Category not found in categories: {c}")
        return values

    @root_validator(allow_reuse=True, skip_on_failure=True)
    def validate_keys(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        for m in values["modules"]:
            keys = m.child_keys()
            keyset = set(keys)
            if len(keys) != len(keyset):
                duplicates: Set[str] = set()
                for key in keys:
                    if key in duplicates:
                        continue
                    elif key not in keyset:
                        duplicates.add(key)
                    else:
                        keyset.remove(key)
                raise ValueError(f"Duplicate learning object (chapter, exercise) keys: {duplicates}")
        return values

    @root_validator(allow_reuse=True, skip_on_failure=True)
    def validate_module_dates(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        end = values.get("end")
        end_dt = _get_datetime(end)
        for i, m in enumerate(values["modules"]):
            close_dt = _get_datetime(m.close)
            if close_dt and end_dt and close_dt > end_dt:
                m.add_warning(f"Course 'end' ({end}) before module {i} 'close' ({m.close})", "close")

            late_close_dt = _get_datetime(m.late_close)
            if late_close_dt and not close_dt and end_dt and late_close_dt < end_dt:
                raise ValueError(f"Module {i} 'late_close' ({m.late_close}) is before 'close' ({end}, defaulted to course 'end')")
        return values

    @root_validator(allow_reuse=True, skip_on_failure=True)
    def validate_course_dates(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        start = values.get("start")
        end = values.get("end")
        start_dt = _get_datetime(start)
        end_dt = _get_datetime(end)

        if start_dt and end_dt and start_dt > end_dt:
            raise ValueError(f"Course 'end' ({end}) before 'start' ({start})")

        return values
