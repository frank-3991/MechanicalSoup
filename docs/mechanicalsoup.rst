The mechanicalsoup package: API documentation
=============================================

.. module:: mechanicalsoup

StatefulBrowser
---------------

.. autoclass:: StatefulBrowser
    :members:
    :undoc-members:
    :show-inheritance:
    :special-members: __setitem__

Browser
-------

.. autoclass:: Browser
    :members:
    :undoc-members:

Form
----

.. autoclass:: Form
    :members:
    :undoc-members:
    :special-members: __setitem__

Recording and replay
--------------------

.. autoclass:: Recorder
    :members:
    :undoc-members:

.. autoclass:: Record
    :members:
    :undoc-members:

.. autofunction:: replay_record

.. autofunction:: load_records

Exceptions
----------
.. autoexception:: LinkNotFoundError
    :show-inheritance:
.. autoexception:: InvalidFormMethod
    :show-inheritance:
.. autoexception:: ReplayError
    :show-inheritance:
.. autoexception:: ReplayIncompleteError
    :show-inheritance:
.. autoexception:: ReplayConfirmationRequired
    :show-inheritance:
