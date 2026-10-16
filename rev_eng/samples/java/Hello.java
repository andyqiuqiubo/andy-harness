public class Hello {
    private String greeting = "Hello, World!";

    public String sayHello(String name) {
        if (name == null || name.isEmpty()) {
            name = greeting;
        }
        return "Hello, " + name;
    }

    public static void main(String[] args) {
        Hello h = new Hello();
        System.out.println(h.sayHello(args.length > 0 ? args[0] : ""));
    }
}
