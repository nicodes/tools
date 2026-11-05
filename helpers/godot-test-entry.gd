extends SceneTree
## Load the test only after Godot has registered and initialized autoloads.
## A test used directly as --script is compiled before those names exist.

func _init() -> void:
	call_deferred("_run_test")

func _run_test() -> void:
	var arguments: PackedStringArray = OS.get_cmdline_user_args()
	if arguments.size() != 1:
		push_error("Expected one res:// test script argument")
		quit(1)
		return
	var test_script: Script = load(arguments[0]) as Script
	if test_script == null or not test_script.can_instantiate():
		push_error("Test script cannot instantiate: %s" % arguments[0])
		quit(1)
		return
	var test_case: Node = test_script.new() as Node
	if test_case == null:
		push_error("Test must extend Node: %s" % arguments[0])
		quit(1)
		return
	root.add_child(test_case)
