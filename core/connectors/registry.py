from core.connectors.base import DataSourceConnector

_REGISTRY: dict[str, type[DataSourceConnector]] = {}

_REQUIRED: tuple[str, ...] = (
    "source_type",
    "source_category",
    "supported_capabilities",
    "config_schema_version",
)


def register(cls: type[DataSourceConnector]) -> type[DataSourceConnector]:
    if not (isinstance(cls, type) and issubclass(cls, DataSourceConnector)):
        raise TypeError(f"{cls} must be a subclass of DataSourceConnector.")
    for attr in _REQUIRED:
        if not hasattr(cls, attr):
            raise TypeError(
                f"{cls.__name__} must define class attribute '{attr}'."
            )
    if not isinstance(cls.source_type, str) or not cls.source_type:
        raise TypeError(f"{cls.__name__}.source_type must be a non-empty str.")
    if not isinstance(cls.source_category, str) or not cls.source_category:
        raise TypeError(f"{cls.__name__}.source_category must be a non-empty str.")
    if not isinstance(cls.supported_capabilities, frozenset):
        raise TypeError(f"{cls.__name__}.supported_capabilities must be a frozenset.")
    if not isinstance(cls.config_schema_version, int) or cls.config_schema_version < 1:
        raise TypeError(
            f"{cls.__name__}.config_schema_version must be a positive int."
        )
    if cls.source_type in _REGISTRY:
        raise ValueError(
            f"source_type '{cls.source_type}' is already registered "
            f"by {_REGISTRY[cls.source_type].__name__}."
        )
    _REGISTRY[cls.source_type] = cls
    return cls


def get(source_type: str) -> type[DataSourceConnector] | None:
    return _REGISTRY.get(source_type)


def list_supported() -> list[str]:
    return sorted(_REGISTRY)


def list_by_category(category: str) -> list[str]:
    return sorted(
        st for st, cls in _REGISTRY.items()
        if cls.source_category == category
    )
