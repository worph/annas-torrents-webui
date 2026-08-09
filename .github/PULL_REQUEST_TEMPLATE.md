name: Pull request
description: Checklist for contributors
body:
  - type: checkboxes
    id: checks
    attributes:
      label: Checklist
      options:
        - label: I read CONTRIBUTING.md
          required: true
        - label: Tests updated or explained why not (unittest / node / playwright as relevant)
          required: true
        - label: CHANGELOG.md Unreleased updated for user-visible changes (or N/A)
          required: false
  - type: textarea
    id: summary
    attributes:
      label: Summary
      description: Why this change exists.
    validations:
      required: true
  - type: textarea
    id: testplan
    attributes:
      label: How you tested
    validations:
      required: true
