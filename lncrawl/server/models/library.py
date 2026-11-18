from typing import Literal, Optional

from pydantic import BaseModel, Field


class LibraryCreateRequest(BaseModel):
    name: str = Field(description="Library name")
    description: Optional[str] = Field(default=None, description="Library description")
    is_public: bool = Field(default=False, description="Is public")


class LibraryUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, description="Library name")
    description: Optional[str] = Field(default=None, description="Library description")
    is_public: Optional[bool] = Field(default=None, description="Is public")


class LibraryItem(BaseModel):
    id: str = Field(description="Library ID")
    name: str = Field(description="Library name")
    description: Optional[str] = Field(default=None, description="Library description")
    is_public: bool = Field(description="Is public")


class LibraryExportRequest(BaseModel):
    save_dir: str = Field(
        min_length=1,
        max_length=1024,
        description="Folder on the machine running the app to write library.zip to, e.g. a "
        "drive with more room; nothing is built on the system drive on the way",
    )
    nsfw: Literal["include", "exclude", "only"] = Field(
        default="include",
        description="Keep NSFW novels (include), leave them out (exclude), or export only them",
    )
    complete_only: bool = Field(
        default=False,
        description="Only novels with every chapter downloaded, in a book that holds them all",
    )


class LibraryUseRequest(BaseModel):
    folder: str = Field(
        min_length=1,
        max_length=1024,
        description="Folder on the machine running the app that already holds a library "
        "(one moved there before); the app runs from it after its next start",
    )


class LibraryMoveRequest(BaseModel):
    folder: str = Field(
        min_length=1,
        max_length=1024,
        description="Folder on the machine running the app to move the whole library to "
        "(chapters, pages, e-books, exports and the database), e.g. another drive",
    )
