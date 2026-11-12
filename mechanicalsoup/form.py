from collections.abc import Mapping
from dataclasses import dataclass, field
import copy
import io
from typing import Any, Callable, Literal
import warnings

import bs4

from .utils import LinkNotFoundError, is_multipart_file_upload


@dataclass
class _ControlIndex:
    inputs: dict[str, bs4.element.Tag] = field(default_factory=dict)
    textareas: dict[str, bs4.element.Tag] = field(default_factory=dict)
    checkboxes: dict[str, list[bs4.element.Tag]] = field(default_factory=dict)
    radios: dict[str, list[bs4.element.Tag]] = field(default_factory=dict)
    selects: dict[str, bs4.element.Tag] = field(default_factory=dict)


@dataclass
class _ControlState:
    tag: bs4.element.Tag
    attrs: dict[str, Any]
    contents: list[Any]


@dataclass
class _Operation:
    apply: Callable[[], None]
    touched: tuple[bs4.element.Tag, ...]


class InvalidFormMethod(LinkNotFoundError):
    """This exception is raised when a method of :class:`Form` is used
    for an HTML element that is of the wrong type (or is malformed).
    It is caught within :func:`Form.set` to perform element type deduction.

    It is derived from :class:`LinkNotFoundError` so that a single base class
    can be used to catch all exceptions specific to this module.
    """
    pass


class Form:
    """Build a fillable form.

    :param form: A bs4.element.Tag corresponding to an HTML form element.

    The Form class is responsible for preparing HTML forms for submission.
    It handles the following types of elements:
    input (text, checkbox, radio), select, and textarea.

    Each type is set by a method named after the type (e.g.
    :func:`~Form.set_select`), and then there are convenience methods (e.g.
    :func:`~Form.set`) that do type-deduction and set the value using the
    appropriate method.

    It also handles submit-type elements using :func:`~Form.choose_submit`.
    """

    def __init__(self, form: bs4.element.Tag):
        if form.name != 'form':
            warnings.warn(
                f"Constructed a Form from a '{form.name}' instead of a 'form' "
                " element. This may be an error in a future version of "
                "MechanicalSoup.", FutureWarning)

        self.form = form
        self._submit_chosen: bs4.element.Tag | Literal[False] | None = None

        # Aliases for backwards compatibility
        # (Included specifically in __init__ to suppress them in Sphinx docs)
        self.attach = self.set_input
        self.input = self.set_input
        self.textarea = self.set_textarea

    @property
    def submit_chosen(self) -> bs4.element.Tag | Literal[False] | None:
        """Get the currently selected submit element."""
        return self._submit_chosen

    def set_input(self, data: Mapping[str, Any]) -> None:
        """Fill-in a set of fields in a form.

        Example: filling-in a login/password form

        .. code-block:: python

           form.set_input({"login": username, "password": password})

        This will find the input element named "login" and give it the
        value ``username``, and the input element named "password" and
        give it the value ``password``.
        """

        for (name, value) in data.items():
            i = self.form.find("input", {"name": name})
            if not i:
                raise InvalidFormMethod("No input field named " + name)
            self._assert_valid_file_upload(i, value)
            i["value"] = value

    def uncheck_all(self, name: str) -> None:
        """Remove the *checked*-attribute of all input elements with
        a *name*-attribute given by ``name``.
        """
        for option in self.form.find_all("input", {"name": name}):
            if "checked" in option.attrs:
                del option.attrs["checked"]

    def check(self, data: Mapping[str, Any]) -> None:
        """For backwards compatibility, this method handles checkboxes
        and radio buttons in a single call. It will not uncheck any
        checkboxes unless explicitly specified by ``data``, in contrast
        with the default behavior of :func:`~Form.set_checkbox`.
        """
        for (name, value) in data.items():
            try:
                self.set_checkbox({name: value}, uncheck_other_boxes=False)
                continue
            except InvalidFormMethod:
                pass
            try:
                self.set_radio({name: value})
                continue
            except InvalidFormMethod:
                pass
            raise LinkNotFoundError("No input checkbox/radio named " + name)

    def set_checkbox(
        self,
        data: Mapping[str, Any],
        uncheck_other_boxes: bool = True,
    ) -> None:
        """Set the *checked*-attribute of input elements of type "checkbox"
        specified by ``data`` (i.e. check boxes).

        :param data: Dict of ``{name: value, ...}``.
            In the family of checkboxes whose *name*-attribute is ``name``,
            check the box whose *value*-attribute is ``value``. All boxes in
            the family can be checked (unchecked) if ``value`` is True (False).
            To check multiple specific boxes, let ``value`` be a tuple or list.
        :param uncheck_other_boxes: If True (default), before checking any
            boxes specified by ``data``, uncheck the entire checkbox family.
            Consider setting to False if some boxes are checked by default when
            the HTML is served.
        """
        for (name, value) in data.items():
            # Case-insensitive search for type=checkbox
            selector = 'input[type="checkbox" i][name="{}"]'.format(name)
            checkboxes = self.form.select(selector)
            if not checkboxes:
                raise InvalidFormMethod("No input checkbox named " + name)

            # uncheck if requested
            if uncheck_other_boxes:
                self.uncheck_all(name)

            # Wrap individual values (e.g. int, str) in a 1-element tuple.
            if not isinstance(value, list) and not isinstance(value, tuple):
                value = (value,)

            # Check or uncheck one or more boxes
            for choice in value:
                choice_str = str(choice)  # Allow for example literal numbers
                for checkbox in checkboxes:
                    if checkbox.attrs.get("value", "on") == choice_str:
                        checkbox["checked"] = ""
                        break
                    # Allow specifying True or False to check/uncheck
                    elif choice is True:
                        checkbox["checked"] = ""
                        break
                    elif choice is False:
                        if "checked" in checkbox.attrs:
                            del checkbox.attrs["checked"]
                        break
                else:
                    raise LinkNotFoundError(
                        "No input checkbox named %s with choice %s" %
                        (name, choice)
                    )

    def set_radio(self, data: Mapping[str, Any]) -> None:
        """Set the *checked*-attribute of input elements of type "radio"
        specified by ``data`` (i.e. select radio buttons).

        :param data: Dict of ``{name: value, ...}``.
            In the family of radio buttons whose *name*-attribute is ``name``,
            check the radio button whose *value*-attribute is ``value``.
            Only one radio button in the family can be checked.
        """
        for (name, value) in data.items():
            # Case-insensitive search for type=radio
            selector = 'input[type="radio" i][name="{}"]'.format(name)
            radios = self.form.select(selector)
            if not radios:
                raise InvalidFormMethod("No input radio named " + name)

            # only one radio button can be checked
            self.uncheck_all(name)

            # Check the appropriate radio button (value cannot be a list/tuple)
            for radio in radios:
                if radio.attrs.get("value", "on") == str(value):
                    radio["checked"] = ""
                    break
            else:
                raise LinkNotFoundError(
                    f"No input radio named {name} with choice {value}"
                )

    def set_textarea(self, data: Mapping[str, Any]) -> None:
        """Set the *string*-attribute of the first textarea element
        specified by ``data`` (i.e. set the text of a textarea).

        :param data: Dict of ``{name: value, ...}``.
            The textarea whose *name*-attribute is ``name`` will have
            its *string*-attribute set to ``value``.
        """
        for (name, value) in data.items():
            t = self.form.find("textarea", {"name": name})
            if not t:
                raise InvalidFormMethod("No textarea named " + name)
            t.string = value

    def set_select(self, data: Mapping[str, Any]) -> None:
        """Set the *selected*-attribute of the first option element
        specified by ``data`` (i.e. select an option from a dropdown).

        :param data: Dict of ``{name: value, ...}``.
            Find the select element whose *name*-attribute is ``name``.
            Then select from among its children the option element whose
            *value*-attribute is ``value``. If no matching *value*-attribute
            is found, this will search for an option whose text matches
            ``value``. If the select element's *multiple*-attribute is set,
            then ``value`` can be a list or tuple to select multiple options.
        """
        for (name, value) in data.items():
            select = self.form.find("select", {"name": name})
            if not select:
                raise InvalidFormMethod("No select named " + name)

            # Deselect all options first
            for option_ in select.find_all("option"):
                if "selected" in option_.attrs:
                    del option_.attrs["selected"]

            # Wrap individual values in a 1-element tuple.
            # If value is a list/tuple, select must be a <select multiple>.
            if not isinstance(value, list) and not isinstance(value, tuple):
                value = (value,)
            elif "multiple" not in select.attrs:
                raise LinkNotFoundError("Cannot select multiple options!")

            for choice in value:
                option = select.find("option", {"value": choice})

                # try to find with text instead of value
                if not option:
                    option = select.find("option", string=choice)

                if not option:
                    raise LinkNotFoundError(
                        f'Option {choice} not found for select {name}'
                    )

                option.attrs["selected"] = "selected"

    def set_values(
        self,
        data: Mapping[str, Any],
        *,
        atomic: bool = False,
    ) -> None:
        """Fill several fields using the same type selection as :func:`set`.

        The controls in ``data`` are processed in mapping order. Building the
        field index requires one traversal of the form, so filling a form is
        linear in the combined number of controls and supplied values.

        :param data: Dict of ``{name: value, ...}``.
        :param atomic: When true, validate every field before applying
            changes. If a field or option does not exist, or a file upload
            value is invalid, the form is restored to its state before this
            call. When false (the default), fields are applied one at a time
            and changes made before an exception remain visible.
        """
        index = self._build_index(data)
        if atomic:
            self._set_values_atomic(data, index)
            return

        for name, value in data.items():
            self._apply_field(name, value, index)

    def _set_values_atomic(
        self,
        data: Mapping[str, Any],
        index: _ControlIndex,
    ) -> None:
        operations: list[_Operation] = []
        state: list[_ControlState] = []
        snapshot_ids: set[int] = set()
        for name, value in data.items():
            apply, touched = self._build_operation(name, value, index)
            operations.append(_Operation(apply, touched))
            for tag in touched:
                if id(tag) not in snapshot_ids:
                    snapshot_ids.add(id(tag))
                    state.append(self._snapshot_control(tag))

        for batch_operation in operations:
            try:
                batch_operation.apply()
            except Exception:
                for snapshot in state:
                    self._restore_control(snapshot)
                raise

    def _build_index(self, data: Mapping[str, Any]) -> _ControlIndex:
        """Build lookup tables only for fields this call needs to fill."""
        wanted = frozenset(data)
        index = _ControlIndex()

        for tag in self.form.find_all(
            ("input", "textarea", "select"),
            attrs={"name": True},
        ):
            name = tag.get("name")
            if name not in wanted:
                continue

            if tag.name == "input":
                input_type = tag.get("type", "")
                if not isinstance(input_type, str):
                    continue

                input_type = input_type.lower()
                if input_type == "checkbox":
                    index.checkboxes.setdefault(name, []).append(tag)
                elif input_type == "radio":
                    index.radios.setdefault(name, []).append(tag)
                else:
                    index.inputs.setdefault(name, tag)
            elif tag.name == "textarea":
                index.textareas.setdefault(name, tag)
            else:
                index.selects.setdefault(name, tag)

        return index

    def _build_operation(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ) -> tuple[Callable[[], None], tuple[bs4.element.Tag, ...]]:
        """Return an operation and the tags it will change."""
        builders = (
            lambda: self._build_checkbox_operation(name, value, index),
            lambda: self._build_radio_operation(name, value, index),
            lambda: self._build_text_input_operation(name, value, index),
            lambda: self._build_textarea_operation(name, value, index),
            lambda: self._build_select_operation(
                name, value, index.selects.get(name)),
        )

        for builder in builders:
            try:
                return builder()
            except InvalidFormMethod:
                continue

        raise LinkNotFoundError("No valid element named " + name)

    def _apply_field(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ) -> None:
        """Apply one field incrementally, preserving legacy partial effects."""
        appliers = (
            lambda: self._apply_checkbox(name, value, index),
            lambda: self._apply_radio(name, value, index),
            lambda: self._apply_text_input(name, value, index),
            lambda: self._apply_textarea(name, value, index),
            lambda: self._apply_select(name, value, index),
        )

        for applier in appliers:
            try:
                applier()
                return
            except InvalidFormMethod:
                continue

        raise LinkNotFoundError("No valid element named " + name)

    def _apply_checkbox(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ) -> None:
        if name not in index.checkboxes:
            raise InvalidFormMethod("No input checkbox named " + name)
        checkboxes = index.checkboxes[name]
        checkboxes_by_value = {
            checkbox.attrs.get("value", "on"): checkbox
            for checkbox in reversed(checkboxes)
        }

        for checkbox in checkboxes:
            if "checked" in checkbox.attrs:
                del checkbox.attrs["checked"]

        for choice in self._as_value_tuple(value):
            if choice is True:
                checkboxes[0]["checked"] = ""
                continue
            if choice is False:
                if "checked" in checkboxes[0].attrs:
                    del checkboxes[0].attrs["checked"]
                continue

            checkbox = checkboxes_by_value.get(str(choice))
            if checkbox is None:
                raise LinkNotFoundError(
                    "No input checkbox named %s with choice %s" %
                    (name, choice)
                )
            checkbox["checked"] = ""

    def _apply_radio(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ) -> None:
        if name not in index.radios:
            raise InvalidFormMethod("No input radio named " + name)
        radios = index.radios[name]

        for radio in radios:
            if "checked" in radio.attrs:
                del radio.attrs["checked"]

        radios_by_value: dict[str, bs4.element.Tag] = {}
        for radio in reversed(radios):
            radios_by_value.setdefault(radio.attrs.get("value", "on"), radio)
        chosen = radios_by_value.get(str(value))
        if chosen is None:
            raise LinkNotFoundError(
                f"No input radio named {name} with choice {value}"
            )
        chosen["checked"] = ""

    def _apply_text_input(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ) -> None:
        if name not in index.inputs:
            raise InvalidFormMethod("No input field named " + name)
        tag = index.inputs[name]
        self._assert_valid_file_upload(tag, value)
        tag["value"] = value

    @staticmethod
    def _apply_textarea(
        name: str,
        value: Any,
        index: _ControlIndex,
    ) -> None:
        if name not in index.textareas:
            raise InvalidFormMethod("No textarea named " + name)
        index.textareas[name].string = value

    def _apply_select(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ) -> None:
        if name not in index.selects:
            raise InvalidFormMethod("No select named " + name)
        select = index.selects[name]
        options = select.find_all("option")

        for option in options:
            if "selected" in option.attrs:
                del option.attrs["selected"]

        choices = self._as_value_tuple(value)
        if isinstance(value, (list, tuple)) and "multiple" not in select.attrs:
            raise LinkNotFoundError("Cannot select multiple options!")

        option_index = self._index_select_options(options)
        for choice in choices:
            option = self._find_select_option(select, choice, option_index)
            if option is None:
                raise LinkNotFoundError(
                    f'Option {choice} not found for select {name}'
                )
            option.attrs["selected"] = "selected"

    def _build_text_input_operation(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ):
        if name not in index.inputs:
            raise InvalidFormMethod("No input field named " + name)
        tag = index.inputs[name]
        self._assert_valid_file_upload(tag, value)

        def operation():
            tag["value"] = value

        return operation, (tag,)

    @staticmethod
    def _build_textarea_operation(
        name: str,
        value: Any,
        index: _ControlIndex,
    ):
        if name not in index.textareas:
            raise InvalidFormMethod("No textarea named " + name)
        tag = index.textareas[name]

        def operation():
            tag.string = value

        return operation, (tag,)

    def _build_checkbox_operation(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ):
        if name not in index.checkboxes:
            raise InvalidFormMethod("No input checkbox named " + name)
        checkboxes = index.checkboxes[name]
        choices = self._as_value_tuple(value)
        checks = {tag: False for tag in checkboxes}
        checkboxes_by_value: dict[str, bs4.element.Tag] = {}
        for checkbox in checkboxes:
            checkboxes_by_value.setdefault(
                checkbox.attrs.get("value", "on"), checkbox)

        for choice in choices:
            if choice is True:
                checks[checkboxes[0]] = True
                continue
            if choice is False:
                checks[checkboxes[0]] = False
                continue

            choice_str = str(choice)
            checkbox = checkboxes_by_value.get(choice_str)
            if checkbox is None:
                raise LinkNotFoundError(
                    "No input checkbox named %s with choice %s" %
                    (name, choice)
                )
            checks[checkbox] = True

        def operation():
            for checkbox, checked in checks.items():
                if checked:
                    checkbox["checked"] = ""
                elif "checked" in checkbox.attrs:
                    del checkbox.attrs["checked"]

        return operation, tuple(checkboxes)

    def _build_radio_operation(
        self,
        name: str,
        value: Any,
        index: _ControlIndex,
    ):
        if name not in index.radios:
            raise InvalidFormMethod("No input radio named " + name)
        radios = index.radios[name]
        choice_str = str(value)
        radios_by_value = {
            radio.attrs.get("value", "on"): radio
            for radio in reversed(radios)
        }
        chosen = radios_by_value.get(choice_str)

        if chosen is None:
            raise LinkNotFoundError(
                f"No input radio named {name} with choice {value}"
            )

        def operation():
            for radio in radios:
                if "checked" in radio.attrs:
                    del radio.attrs["checked"]
            chosen["checked"] = ""

        return operation, tuple(radios)

    def _build_select_operation(
        self,
        name: str,
        value: Any,
        select: bs4.element.Tag | None,
    ):
        if select is None:
            raise InvalidFormMethod("No select named " + name)
        options = select.find_all("option")
        choices = self._as_value_tuple(value)
        if isinstance(value, (list, tuple)) and "multiple" not in select.attrs:
            raise LinkNotFoundError("Cannot select multiple options!")

        option_index = self._index_select_options(options)
        chosen_options = []
        for choice in choices:
            option = self._find_select_option(select, choice, option_index)
            if option is None:
                raise LinkNotFoundError(
                    f'Option {choice} not found for select {name}'
                )
            chosen_options.append(option)

        def operation():
            for option in options:
                if "selected" in option.attrs:
                    del option.attrs["selected"]
            for option in chosen_options:
                option.attrs["selected"] = "selected"

        return operation, tuple(options)

    @staticmethod
    def _find_select_option(
        select,
        choice,
        option_index,
    ):
        (options_by_value, options_without_value, options_by_text,
         first_option, first_option_with_value) = option_index
        try:
            if choice is None or choice is False:
                option = (
                    options_without_value[0]
                    if options_without_value else
                    first_option
                )
            elif choice == "":
                option = (
                    options_without_value[0]
                    if options_without_value else
                    options_by_value.get("", first_option)
                )
            elif choice is True:
                option = first_option_with_value or first_option
            else:
                option = options_by_value.get(
                    choice,
                    options_by_value.get(
                        str(choice),
                        options_by_text.get(choice),
                    ),
                )
        except TypeError:
            option = select.find("option", {"value": choice})
            if option is None:
                option = select.find("option", string=choice)
        return option

    @staticmethod
    def _index_select_options(options):
        options_by_value = {}
        options_without_value = []
        options_by_text = {}
        first_option = options[0] if options else None
        first_option_with_value = None
        for option in options:
            if option.has_attr("value"):
                value = option["value"]
                options_by_value.setdefault(value, option)
                options_by_value.setdefault(str(value), option)
                if first_option_with_value is None:
                    first_option_with_value = option
            else:
                options_without_value.append(option)
            options_by_text.setdefault(option.string, option)
        return (
            options_by_value,
            options_without_value,
            options_by_text,
            first_option,
            first_option_with_value,
        )

    @staticmethod
    def _as_value_tuple(value):
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return (value,)

    @staticmethod
    def _snapshot_control(tag):
        return _ControlState(
            tag=tag,
            attrs=dict(tag.attrs),
            contents=copy.deepcopy(tag.contents),
        )

    @staticmethod
    def _restore_control(snapshot):
        snapshot.tag.attrs = dict(snapshot.attrs)
        snapshot.tag.clear()
        for content in copy.deepcopy(snapshot.contents):
            snapshot.tag.append(content)

    def __setitem__(self, name: str, value: Any) -> None:
        """Forwards arguments to :func:`~Form.set`. For example,
        :code:`form["name"] = "value"` calls :code:`form.set("name", "value")`.
        """
        return self.set(name, value)

    def set(self, name: str, value: Any, force: bool = False) -> None:
        """Set a form element identified by ``name`` to a specified ``value``.
        The type of element (input, textarea, select, ...) does not
        need to be given; it is inferred by the following methods:
        :func:`~Form.set_checkbox`,
        :func:`~Form.set_radio`,
        :func:`~Form.set_input`,
        :func:`~Form.set_textarea`,
        :func:`~Form.set_select`.
        If none of these methods find a matching element, then if ``force``
        is True, a new element (``<input type="text" ...>``) will be
        added using :func:`~Form.new_control`.

        Example: filling-in a login/password form with EULA checkbox

        .. code-block:: python

            form.set("login", username)
            form.set("password", password)
            form.set("eula-checkbox", True)

        Example: uploading a file through a ``<input type="file"
        name="tagname">`` field (provide an open file object,
        and its content will be uploaded):

        .. code-block:: python

            form.set("tagname", open(path_to_local_file, "rb"))

        """
        for func in ("checkbox", "radio", "input", "textarea", "select"):
            try:
                getattr(self, "set_" + func)({name: value})
                return
            except InvalidFormMethod:
                pass
        if force:
            self.new_control('text', name, value=value)
            return
        raise LinkNotFoundError("No valid element named " + name)

    def new_control(
        self,
        type: str,
        name: str,
        value: Any,
        **kwargs: Any,
    ) -> bs4.element.Tag:
        """Add a new input element to the form.

        The arguments set the attributes of the new element.
        """
        # Remove existing input-like elements with the same name
        for tag in ('input', 'textarea', 'select'):
            for old in self.form.find_all(tag, {'name': name}):
                old.decompose()
        # We don't have access to the original soup object (just the
        # Tag), so we instantiate a new BeautifulSoup() to call
        # new_tag(). We're only building the soup object, not parsing
        # anything, so the parser doesn't matter. Specify the one
        # included in Python to avoid having dependency issue.
        control = bs4.BeautifulSoup("", "html.parser").new_tag('input')
        control['type'] = type
        control['name'] = name
        control['value'] = value
        for k, v in kwargs.items():
            control[k] = v
        self._assert_valid_file_upload(control, value)
        self.form.append(control)
        return control

    def choose_submit(
        self,
        submit: bs4.element.Tag | str | Literal[False] | None,
    ) -> bs4.element.Tag | Literal[False] | None:
        """Selects the input (or button) element to use for form submission.

        :param submit: The :class:`bs4.element.Tag` (or just its
            *name*-attribute) that identifies the submit element to use. If
            ``None``, will choose the first valid submit element in the form,
            if one exists. If ``False``, will not use any submit element;
            this is useful for simulating AJAX requests, for example.

        To simulate a normal web browser, only one submit element must be
        sent. Therefore, this does not need to be called if there is only
        one submit element in the form.

        If the element is not found or if multiple elements match, raise a
        :class:`LinkNotFoundError` exception.

        Example: ::

            browser = mechanicalsoup.StatefulBrowser()
            browser.open(url)
            form = browser.select_form()
            form.choose_submit('form_name_attr')
            browser.submit_selected()
        """
        if submit is False:
            self._submit_chosen = False
            return self._submit_chosen

        # All buttons NOT of type (button,reset) are valid submits
        # Case-insensitive search for type=submit
        inps = [
            inp for inp in self.form.select('input[type="submit" i], button')
            if (
                not isinstance(inp_type := inp.get("type"), str)
                or inp_type.lower() not in ('button', 'reset')
            )
        ]

        submit_chosen = None

        # If no submit specified, choose the first one
        if submit is None and inps:
            submit_chosen = inps[0]

        for inp in inps:
            if (inp.has_attr('name') and inp['name'] == submit):
                # Found the tag element by name
                if submit_chosen is not None:
                    raise LinkNotFoundError(
                        f"Multiple submit elements match: {submit}"
                    )
                submit_chosen = inp
            elif inp == submit:
                # Found the exact input tag element
                submit_chosen = inp
                break

        if submit_chosen is None and submit not in (None, False):
            raise LinkNotFoundError(
                f"Specified submit element not found: {submit}"
            )
        self._submit_chosen = submit_chosen

        return self._submit_chosen

    def print_summary(self) -> None:
        """Print a summary of the form.

        May help finding which fields need to be filled-in.
        """
        for input in self.form.find_all(
                ("input", "textarea", "select", "button")):
            input_copy = copy.copy(input)
            # Text between the opening tag and the closing tag often
            # contains a lot of spaces that we don't want here.
            for subtag in input_copy.find_all() + [input_copy]:
                if subtag.string:
                    subtag.string = subtag.string.strip()
            print(input_copy)

    def _assert_valid_file_upload(
        self,
        tag: bs4.element.Tag,
        value: Any,
    ) -> None:
        """Raise an exception if a multipart file input is not an open file."""
        if (
            is_multipart_file_upload(self.form, tag) and
            not isinstance(value, io.IOBase)
        ):
            raise ValueError(
                "From v1.3.0 onwards, you must pass an open file object "
                'directly, e.g. `form["name"] = open("/path/to/file", "rb")`. '
                "This change is to remediate a security vulnerability where "
                "a malicious web server could read arbitrary files from the "
                "client (CVE-2023-34457)."
            )
