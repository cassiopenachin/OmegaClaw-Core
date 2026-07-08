!(import! &self (library lib_import))
!(if (== () (collapse (callPredicate (Predicate (exists_directory "./repos/OmegaClaw-Core")))))
     (progn (println! "FATAL: OmegaClaw-Core is not vendored at ./repos/OmegaClaw-Core. This fork does not fetch dependencies at boot; clone it into ./repos (see README) or use the built image.")
            (callPredicate (Predicate (halt 1))))
     (assertaPredicate (Predicate (library_path "./repos/OmegaClaw-Core"))))
!(import! &self (library OmegaClaw-Core lib_omegaclaw))

!(omegaclaw)
