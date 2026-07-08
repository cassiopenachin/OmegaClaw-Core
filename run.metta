!(import! &self (library lib_import))
!(assertaPredicate (Predicate (library_path "./repos/OmegaClaw-Core")))
!(import! &self (library OmegaClaw-Core lib_omegaclaw))

!(omegaclaw)
